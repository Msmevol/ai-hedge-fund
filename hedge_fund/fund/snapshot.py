"""FundSnapshot — what the fund analyst is allowed to know about a Chinese
public mutual fund (场外公募基金).

A FundSnapshot is a plain data view, built by FundClient from 天天基金
(eastmoney) public web endpoints. It carries no logic; the analyst LLM
reasons over its rendered text.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class FundHolding:
    """One stock from the fund's latest quarterly top-10 report."""

    code: str
    name: str
    percent: float | None  # 占净值比例 (%)


@dataclass(frozen=True)
class FundSnapshot:
    """A domestic mutual fund as seen by the analyst.

    Every metric is `None` when the source did not provide it — a missing
    field renders as "数据缺失" instead of crashing the analysis.
    """

    code: str
    name: str
    fund_type: str | None  # 股票型 / 混合型 / 指数型 / 债券型
    inception: str | None  # 成立日期 YYYY-MM-DD
    scale_billion: float | None  # 最新规模（亿元）
    purchase_fee: float | None  # 申购费率 (%)
    mgmt_fee: float | None  # 管理费率 (% 每年)
    custody_fee: float | None  # 托管费率 (% 每年)
    return_1y: float | None  # 近 1 年收益 (倍数, e.g. 0.10 = +10%)
    return_3y: float | None  # 近 3 年收益 (倍数)
    return_5y: float | None  # 近 5 年收益 (倍数)
    ytd: float | None  # 今年以来收益 (倍数)
    max_drawdown: float | None  # 历史最大回撤 (负数, 0 = 无回撤)
    holdings: tuple[FundHolding, ...] = field(default_factory=tuple)
    manager: str | None = None  # 现任基金经理姓名
    manager_tenure: str | None = None  # 从业时长描述，如 "13年327天"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def content_hash(self) -> str:
        """Hash of everything the analyst sees — unchanged snapshot, no
        second LLM call (same contract as FundamentalsSnapshot)."""
        body = {
            "code": self.code,
            "name": self.name,
            "fund_type": self.fund_type,
            "inception": self.inception,
            "scale_billion": self.scale_billion,
            "purchase_fee": self.purchase_fee,
            "mgmt_fee": self.mgmt_fee,
            "custody_fee": self.custody_fee,
            "return_1y": self.return_1y,
            "return_3y": self.return_3y,
            "return_5y": self.return_5y,
            "ytd": self.ytd,
            "max_drawdown": self.max_drawdown,
            "holdings": [(h.code, h.name, h.percent) for h in self.holdings],
            "manager": self.manager,
            "manager_tenure": self.manager_tenure,
        }
        return hashlib.sha1(
            json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]

    # ------------------------------------------------------------------
    # Rendering — the analyst's user prompt
    # ------------------------------------------------------------------

    def render(self) -> str:
        """A Chinese, analyst-facing text view of the fund."""
        line = "数据缺失"

        def pct(v: float | None, *, signed: bool = False) -> str:
            if v is None:
                return line
            sign = "+" if signed and v > 0 else ""
            return f"{sign}{v * 100:.1f}%"

        def money(v: float | None) -> str:
            return f"{v:.1f}亿元" if v is not None else line

        def fee(v: float | None) -> str:
            return f"{v:.2f}%" if v is not None else line

        head = (f"基金: {self.name}（{self.code}）"
                f" 类型: {self.fund_type or line}")
        if self.inception:
            head += f" 成立: {self.inception}"

        perf = (
            f"业绩: 近1年 {pct(self.return_1y, signed=True)} | "
            f"近3年 {pct(self.return_3y, signed=True)} | "
            f"近5年 {pct(self.return_5y, signed=True)} | "
            f"今年以来 {pct(self.ytd, signed=True)} | "
            f"最大回撤 {pct(self.max_drawdown)}"
        )
        fee_line = (
            f"费率: 申购 {fee(self.purchase_fee)} | "
            f"管理 {fee(self.mgmt_fee)} | 托管 {fee(self.custody_fee)}"
        )
        body = f"规模: {money(self.scale_billion)} | {fee_line}"

        if self.manager:
            tenure = f"（从业约 {self.manager_tenure}）" if self.manager_tenure else ""
            body += f" | 基金经理: {self.manager}{tenure}"

        lines = [head, body, perf]
        if self.holdings:
            def hpct(v: float | None) -> str:
                return f"{v:.1f}%" if v is not None else line
            top = " | ".join(
                f"{h.name} {hpct(h.percent)}" for h in self.holdings[:5]
            )
            lines.append(f"前十大重仓（最新季报，前5名）: {top}")
        else:
            lines.append("前十大重仓: 数据缺失")
        return "\n".join(lines)