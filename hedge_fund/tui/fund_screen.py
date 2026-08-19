"""FundPickerScreen — 基金选购：内置主题 → 候选池 → 逐只快照并让基金分析师
打分 → 排序报告（推荐买入 / 可观望 / 不建议）。

一台独立的“分析工作台”，与股票开船（RunScreen）互不干扰：这里只读天天基金
公开数据 + LLM 推理，不产生订单、不动回测引擎。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.console import Group
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, OptionList, Static
from textual.widgets.option_list import Option

from hedge_fund.data.fund_client import THEMES
from hedge_fund.fund import FundSnapshot
from hedge_fund.signals import FundVerdict
from hedge_fund.tui.shared import BRIGHT, GREEN, MUTED, RED, TEXT
from hedge_fund.web.analysis import _snapshot_from_dict, run_fund_analysis

_MARK_LABEL = {"buy": "推荐买入", "hold": "可观望", "avoid": "不建议"}
_MARK_STYLE = {"buy": f"bold {GREEN}", "hold": "bold #facc15", "avoid": f"bold {RED}"}


class FundPickerScreen(Screen):
    """Pick a theme, wait for the desk, read the ranked verdicts."""

    BINDINGS = [
        Binding("escape", "back", "返回"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._phase = "idle"  # idle → running → done | failed
        self._verdicts: list[FundVerdict] = []
        self._snapshots: dict[str, FundSnapshot] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-hero"):
            yield Static(Text.assemble(
                ("基 金 选 购\n", f"bold {BRIGHT}"),
                ("国内场外公募基金 · 选主题，让分析师逐个打分", MUTED),
            ), id="picker-title")
            yield OptionList(id="picker-themes")
            yield Static("", id="picker-status")
        with Horizontal(id="picker-main"):
            with VerticalScroll(id="picker-list"):
                yield OptionList(id="picker-report")
            with VerticalScroll(id="picker-detail"):
                yield Static("", id="picker-detail-body")
        yield Footer()

    def on_mount(self) -> None:
        menu = self.query_one("#picker-themes", OptionList)
        menu.clear_options()
        for theme in THEMES:
            menu.add_option(Option(theme, id=f"theme:{theme}"))
        menu.focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:
        if action == "back":
            return True
        return super().check_action(action, parameters)

    def action_back(self) -> None:
        self.app.pop_screen()

    # ------------------------------------------------------------------
    # Theme → run
    # ------------------------------------------------------------------

    @on(OptionList.OptionSelected, "#picker-themes")
    def _choose_theme(self, event: OptionList.OptionSelected) -> None:
        theme = (event.option.id or "").split(":", 1)[1]
        if self._phase == "running":
            self.notify("已有分析在进行中", title="基金选购", severity="warning")
            return
        self.run_picker(theme)

    @work(thread=True)
    def run_picker(self, theme: str) -> None:
        app = self.app
        self._phase = "running"
        self._verdicts = []
        self._snapshots = {}
        self._theme = theme
        self.query_one("#picker-status", Static).update(
            Text.assemble(("⏳ ", "bold #facc15"),
                          (f"正在拉取「{theme}」主题基金池…", MUTED)))
        self.query_one("#picker-report", OptionList).clear_options()
        self.query_one("#picker-detail-body", Static).update("")
        try:
            run_fund_analysis(
                theme,
                lambda ev: app.call_from_thread(self._on_event, ev))
        except Exception as exc:  # pool-level failure: fail loud in the UI
            app.call_from_thread(self._fail, f"{type(exc).__name__}: {exc}")

    def _on_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "pool":
            self._set_status(
                f"候选池 {event['count']} 只（规模≥5亿、成立≥3年），正在逐个分析…")
        elif kind == "fund_done":
            self._verdicts.append(FundVerdict(
                code=event["code"], name=event["name"], signal=event["signal"],
                confidence=event["confidence"], reasoning=event["reasoning"]))
            snap = event.get("snapshot")
            if snap:
                self._snapshots[event["code"]] = _snapshot_from_dict(snap)
            self._set_status(f"已分析 {event['done']}/{event['total']} 只…")
        elif kind == "done":
            by_code = {v.code: v for v in self._verdicts}
            ranked = [by_code[item["code"]] for item in event["order"]]
            self._show_report(self._theme, ranked)

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------

    def _show_report(self, theme: str, verdicts: list[FundVerdict]) -> None:
        self._phase = "done"
        ranked = _sort_verdicts(verdicts)
        self._verdicts = ranked
        menu = self.query_one("#picker-report", OptionList)
        menu.clear_options()
        for v in ranked:
            label = _MARK_LABEL[v.mark]
            style = _MARK_STYLE[v.mark]
            menu.add_option(Option(
                Text.assemble(
                    (f"{v.rank_order:>2}. ", MUTED),
                    (f"{v.code} ", "bold"),
                    (f"{v.name}  ", ""),
                    (f"{label} ", style),
                    (f"{v.confidence:.0f}%", style),
                    ("  ·  ", MUTED),
                    (v.reasoning.split("。")[0] + "。", ""),
                ),
                id=f"verdict:{v.rank_order}"))
        self.query_one("#picker-status", Static).update(Text.assemble(
            ("✓ ", f"bold {GREEN}"),
            (f"「{theme}」分析完成：共 {len(ranked)} 只，按推荐度排序。"
             f"回车展开理由，Esc 返回。", MUTED)))
        menu.highlighted = 0
        menu.focus()
        self._show_detail(0)

    @on(OptionList.OptionHighlighted, "#picker-report")
    def _hover(self, event: OptionList.OptionHighlighted) -> None:
        oid = (event.option.id or "") if event.option else ""
        if oid.startswith("verdict:"):
            self._show_detail(int(oid.split(":")[1]) - 1)

    def _show_detail(self, i: int) -> None:
        if not self._verdicts:
            return
        v = self._verdicts[i]
        snap = self._snapshots.get(v.code)
        body: list[Text] = [
            Text.assemble(
                (f"{v.name}（{v.code}）", f"bold {BRIGHT}"), ("  ", ""),
                (v.label, _MARK_STYLE[v.mark]), (f"  {v.confidence:.0f}%", _MARK_STYLE[v.mark])),
        ]
        if snap is not None:
            body.append(Text("─" * 60, style="dim"))
            for line in snap.render().splitlines():
                body.append(Text(line, style=TEXT))
        body.append(Text("─" * 60, style="dim"))
        if v.reasoning:
            body.append(Text.assemble(("理由: ", "bold"), (v.reasoning, "")))
        self.query_one("#picker-detail-body", Static).update(Group(*body))

    def _set_status(self, message: str) -> None:
        self.query_one("#picker-status", Static).update(
            Text(message, style=MUTED))

    def _fail(self, message: str) -> None:
        self._phase = "failed"
        self.query_one("#picker-status", Static).update(
            Text.assemble(("✗ ", f"bold {RED}"), (message, RED)))
        self.notify(message, title="基金选购失败", severity="error")


def _fuse(llm: FundVerdict, quant) -> FundVerdict:
    """Fuse the LLM verdict and the quant score into one report verdict.

    Same direction → that signal, confidence = the stronger of the two.
    Direct clash (bullish vs bearish) → neutral at average confidence.
    A quant abstention (insufficient history) or an LLM abstention
    (unparseable/LLM failure) falls back to whichever side has a view.
    """
    if quant.signal == "neutral" and quant.confidence == 0.0:
        return llm
    if llm.confidence == 0.0 and "无法分析" in llm.reasoning:
        return FundVerdict(
            code=llm.code, name=llm.name, signal=quant.signal,
            confidence=quant.confidence,
            reasoning=f"【量化】{quant.reasoning}\n【LLM】{llm.reasoning}")
    if llm.signal == quant.signal:
        return FundVerdict(
            code=llm.code, name=llm.name, signal=llm.signal,
            confidence=max(llm.confidence, quant.confidence),
            reasoning=f"【量化】{quant.reasoning}\n【LLM】{llm.reasoning}")
    if {llm.signal, quant.signal} == {"bullish", "bearish"}:
        return FundVerdict(
            code=llm.code, name=llm.name, signal="neutral",
            confidence=round((llm.confidence + quant.confidence) / 2, 1),
            reasoning=f"【量化】{quant.reasoning}\n【LLM】{llm.reasoning}（与量化分歧，按观望处理）")
    # exactly one side has a view — take the side that has one
    viewed = llm if llm.signal != "neutral" else None
    viewed = viewed or (quant if quant.signal != "neutral" else None)
    if viewed is None:
        return llm
    if viewed is quant:
        return FundVerdict(
            code=llm.code, name=llm.name, signal=quant.signal,
            confidence=quant.confidence,
            reasoning=f"【量化】{quant.reasoning}\n【LLM】{llm.reasoning}")
    return llm