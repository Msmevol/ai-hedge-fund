"""FundQuantModel — 基金版量化信号，与 LLM 分析师并列（项目 PEAD 的基金侧对应物）。

六路成分（每路归一到 -1..+1），加权合成总分：
- 动量    (0.30): 近12月收益 − 近1月收益（剔除短期反转）
- Calmar  (0.25): 年化收益 / 同窗口最大回撤
- Alpha   (0.20): 近36月对沪深300 的回归超额（Jensen's α 年化）
- 集中度  (0.10): 前十大重仓占比（过高=风险，过低=分散，中间=中性）
- 规模    (0.10): <2亿 清盘风险、>200亿 超额衰减
- 费率    (0.05): 申购+管理+托管 合计过高扣分

数据不足时对应的成分权重重新归一化；月收益不足 12 个月 → 弃权
（neutral, confidence 0），绝不臆造历史。

合成信号与置信度：
    total ≥ +0.20 → bullish；≤ -0.20 → bearish；否则 neutral
    confidence = min(90, 50 + |total| × 60)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import prod, tanh

from hedge_fund.fund import FundSnapshot

_WEIGHTS = {
    "momentum": 0.30,
    "calmar": 0.25,
    "alpha": 0.20,
    "concentration": 0.10,
    "scale": 0.10,
    "fee": 0.05,
}
_BULLISH_AT = 0.20


@dataclass(frozen=True)
class FundQuantResult:
    """One quant verdict; `components` holds the six normalized scores,
    `raw` the underlying numbers for the report's detail view."""

    signal: str  # bullish / neutral / bearish
    confidence: float
    total: float
    components: dict[str, float] = field(default_factory=dict)
    raw: dict[str, float | None] = field(default_factory=dict)
    reasoning: str = ""


class FundQuantModel:
    """Pure math over a snapshot + monthly returns; no I/O, no LLM."""

    name = "fund_quant"

    def score(self, snapshot: FundSnapshot, fund_monthly: tuple[float, ...],
              bench_monthly: tuple[float, ...]) -> FundQuantResult:
        scores: dict[str, float] = {}
        raw: dict[str, float | None] = {}
        weighted: dict[str, float] = {}

        n = len(fund_monthly)
        if n < 12:
            return FundQuantResult(
                signal="neutral", confidence=0.0, total=0.0,
                reasoning=f"历史数据不足（仅 {n} 个月），量化信号弃权。")

        # --- momentum: 12m − 1m -------------------------------------------
        p12 = prod(1 + r for r in fund_monthly[-12:]) - 1.0
        p1 = prod(1 + r for r in fund_monthly[-1:]) - 1.0
        mom = p12 - p1
        scores["momentum"] = tanh(mom * 3)
        raw["momentum_12m"] = p12
        raw["momentum_1m"] = p1
        weighted["momentum"] = scores["momentum"]

        # --- calmar: annualized return over same-window drawdown ----------
        window = fund_monthly[-36:] if n >= 36 else fund_monthly
        ann = prod(1 + r for r in window) ** (12 / len(window)) - 1.0
        mdd = _max_dd(window)
        if mdd and mdd < 0:
            calmar = ann / abs(mdd)
            scores["calmar"] = tanh(calmar / 2)
        else:
            calmar = None  # no drawdown at all — flawless ride, reward it
            scores["calmar"] = 0.9 if ann > 0 else -0.9
        raw["annualized"] = ann
        raw["drawdown"] = mdd
        weighted["calmar"] = scores["calmar"]

        # --- alpha: OLS excess return vs 沪深300 (36m) ---------------------
        if n >= 36 and len(bench_monthly) >= 36:
            f = fund_monthly[-36:]
            b = bench_monthly[-36:]
            alpha_m = _ols_alpha(f, b)
            alpha_ann = (1 + alpha_m) ** 12 - 1.0
            scores["alpha"] = tanh(alpha_ann * 3)
            raw["alpha_annualized"] = alpha_ann
            weighted["alpha"] = scores["alpha"]

        # --- concentration: top-10 NAV share -------------------------------
        top = [h.percent for h in snapshot.holdings if h.percent is not None]
        conc = sum(top) / 100.0 if top else None
        if conc is None:
            scores["concentration"] = 0.0
        elif conc > 0.6:
            scores["concentration"] = -(conc - 0.6) / 0.4
        elif conc < 0.3:
            scores["concentration"] = (0.3 - conc) / 0.3
        else:
            scores["concentration"] = 0.0
        raw["concentration"] = conc
        weighted["concentration"] = scores["concentration"]

        # --- scale ----------------------------------------------------------
        scale = snapshot.scale_billion
        if scale is None:
            scores["scale"] = 0.0
        elif scale < 2:
            scores["scale"] = -1.0
        elif scale < 5:
            scores["scale"] = -0.5
        elif scale <= 50:
            scores["scale"] = 0.0
        elif scale <= 200:
            scores["scale"] = 0.3
        else:
            scores["scale"] = -0.5
        raw["scale_billion"] = scale
        weighted["scale"] = scores["scale"]

        # --- fees ------------------------------------------------------------
        fees = [f for f in (snapshot.purchase_fee, snapshot.mgmt_fee,
                            snapshot.custody_fee) if f is not None]
        fee_total = sum(fees) if fees else None
        if fee_total is None:
            scores["fee"] = 0.0
        elif fee_total <= 1.8:
            scores["fee"] = 0.3
        elif fee_total <= 2.2:
            scores["fee"] = 0.0
        else:
            scores["fee"] = -0.5
        raw["fee_total"] = fee_total
        weighted["fee"] = scores["fee"]

        # --- combine ---------------------------------------------------------
        total = sum(_WEIGHTS[k] * v for k, v in weighted.items())
        if total >= _BULLISH_AT:
            signal = "bullish"
        elif total <= -_BULLISH_AT:
            signal = "bearish"
        else:
            signal = "neutral"
        confidence = min(90.0, 50.0 + abs(total) * 60.0)
        reasoning = _reasoning(snapshot, signal, total, raw)
        return FundQuantResult(signal=signal, confidence=round(confidence, 1),
                               total=round(total, 3),
                               components=scores, raw=raw,
                               reasoning=reasoning)


def _max_dd(monthly: tuple[float, ...]) -> float | None:
    """Drawdown depth of a monthly return series (negative)."""
    wealth = 1.0
    peak = 1.0
    mdd = 0.0
    for r in monthly:
        wealth *= 1 + r
        peak = max(peak, wealth)
        mdd = min(mdd, wealth / peak - 1.0)
    return mdd if mdd < 0 else None


def _ols_alpha(fund: list[float], bench: list[float]) -> float:
    """Monthly Jensen's alpha (intercept of fund~bench OLS)."""
    n = len(fund)
    b_mean = sum(bench) / n
    f_mean = sum(fund) / n
    cov = sum((b - b_mean) * (f - f_mean) for b, f in zip(bench, fund))
    var = sum((b - b_mean) ** 2 for b in bench)
    if var <= 0:
        return 0.0  # flat benchmark: market exposure unestimable, no alpha
    beta = cov / var
    return f_mean - beta * b_mean


def _reasoning(snapshot: FundSnapshot, signal: str, total: float,
               raw: dict) -> str:
    def pct(v) -> str:
        return "—" if v is None else f"{v * 100:+.1f}%"

    label = {"bullish": "推荐买入", "neutral": "可观望",
             "bearish": "不建议"}[signal]
    ann = raw.get("annualized")
    dd = raw.get("drawdown")
    calmar = (f"{ann * 100:+.1f}%年化/{abs(dd) * 100:.0f}%回撤"
              if ann is not None and dd is not None else "—")
    conc = raw.get("concentration")
    conc_s = f"{conc * 100:.0f}%" if conc is not None else "—"
    scale = raw.get("scale_billion")
    scale_s = f"{scale:.1f}亿元" if scale is not None else "—"
    fee = raw.get("fee_total")
    fee_s = f"{fee:.2f}%" if fee is not None else "—"
    parts = [
        f"量化综合 {total:+.2f} → {label}",
        f"动量: {pct(raw.get('momentum_12m'))}(12m) {pct(raw.get('momentum_1m'))}(1m)",
        f"Calmar: {calmar}",
        f"Alpha年化: {pct(raw.get('alpha_annualized'))}",
        f"集中度: {conc_s}",
        f"规模: {scale_s} | 费率: {fee_s}",
    ]
    return " | ".join(parts)