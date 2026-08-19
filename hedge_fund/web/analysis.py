"""Shared fund-analysis pipeline — the same end-to-end flow the TUI picker
screen and the web app both run, so their verdicts can never drift apart.

A theme in, a stream of plain-dict events out:

    {"type": "pool",  "theme", "count", "funds": [...]}
    {"type": "fund_done", "code", "name", "signal", "confidence",
     "reasoning", "quant": {...}, "snapshot": {...}}     (one per fund)
    {"type": "done",  "total", "order": [{"code", "rank", "signal",
     "label"}, ...]}

`run_fund_analysis(theme, on_event)` runs the pool fetch serially, then the
per-fund work (snapshot + quant + LLM + fuse) on a 3-worker pool, calling
``on_event`` from those worker threads as each fund finishes. A per-fund
failure emits a neutral abstention instead of raising; a pool-level failure
raises (the caller decides how to surface it).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from hedge_fund.data.fund_client import FundClient, FundInfo
from hedge_fund.fund import FundSnapshot
from hedge_fund.signals import (
    FundAnalyst,
    FundQuantModel,
    FundQuantResult,
    FundVerdict,
    _sort_verdicts,
)

_MAX_WORKERS = 3


def run_fund_analysis(theme: str, on_event) -> None:
    """Fetch the pool, analyze every fund, stream events to ``on_event``.

    ``on_event`` receives plain dicts and is called from worker threads —
    thread-safe marshalling (e.g. ``app.call_from_thread``) is the caller's
    job. Raises on pool-level failure; per-fund failures abstain.
    """
    client = FundClient()
    try:
        pool = client.list_funds(theme)
    finally:
        client.close()

    if not pool:
        raise ValueError(f"「{theme}」主题下没有符合条件的基金。")

    on_event({
        "type": "pool",
        "theme": theme,
        "count": len(pool),
        "funds": [asdict(info) for info in pool],
    })

    verdicts: list[FundVerdict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(pool))) as ex:
        futures = {ex.submit(_one, info): info for info in pool}
        for info in pool:
            on_event({"type": "fund_start", "code": info.code,
                      "name": info.name})
        for future in as_completed(futures):
            done += 1
            info = futures[future]
            try:
                verdict, snapshot, quant = future.result()
            except Exception as exc:
                verdict = FundVerdict(
                    code=info.code, name=info.name, signal="neutral",
                    confidence=0.0,
                    reasoning=f"无法分析（{type(exc).__name__}）")
                snapshot, quant = None, None
            verdicts.append(verdict)
            on_event({
                "type": "fund_done",
                "done": done,
                "total": len(pool),
                "code": verdict.code,
                "name": verdict.name,
                "signal": verdict.signal,
                "confidence": verdict.confidence,
                "reasoning": verdict.reasoning,
                "quant": asdict(quant) if quant is not None else None,
                "snapshot": _snapshot_dict(snapshot) if snapshot else None,
            })

    ranked = _sort_verdicts(verdicts)
    on_event({
        "type": "done",
        "total": len(pool),
        "order": [{"code": v.code, "rank": v.rank_order,
                   "signal": v.signal, "label": v.label}
                  for v in ranked],
    })


def _one(info: FundInfo) -> tuple[FundVerdict, FundSnapshot, FundQuantResult]:
    """Snapshot + quant + LLM + fuse for one fund (own client, own analyst)."""
    client = FundClient()
    try:
        snapshot = client.fetch_snapshot(info)
        qi = client.fetch_quant_input(info.code)
        quant = FundQuantModel().score(snapshot, qi.fund_monthly,
                                       qi.bench_monthly)
        verdict = fuse(FundAnalyst().analyze(snapshot), quant)
        return verdict, snapshot, quant
    finally:
        client.close()


def fuse(llm: FundVerdict, quant: FundQuantResult) -> FundVerdict:
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


def _snapshot_dict(snapshot: FundSnapshot) -> dict:
    """The snapshot as a plain JSON-able dict (holdings as list of dicts)."""
    data = asdict(snapshot)
    data["holdings"] = [
        {"code": h.code, "name": h.name, "percent": h.percent}
        for h in snapshot.holdings
    ]
    return data


def _snapshot_from_dict(data: dict) -> FundSnapshot:
    """Inverse of :func:`_snapshot_dict` — rebuild the frozen dataclass."""
    from hedge_fund.fund.snapshot import FundHolding

    holdings = tuple(
        FundHolding(h["code"], h["name"], h.get("percent"))
        for h in data.get("holdings") or []
    )
    return FundSnapshot(**{**data, "holdings": holdings})