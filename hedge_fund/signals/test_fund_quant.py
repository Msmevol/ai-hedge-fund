"""FundQuantModel tests — pure math on canned series, no network."""

import pytest

from hedge_fund.fund import FundHolding, FundSnapshot
from hedge_fund.signals import FundQuantModel


def _snap(**overrides):
    base = dict(
        code="010013", name="科技基金", fund_type="股票型",
        inception="2020-08-24", scale_billion=152.0,
        purchase_fee=1.5, mgmt_fee=1.2, custody_fee=0.2,
        return_1y=1.1, return_3y=2.4, return_5y=None, ytd=0.57,
        max_drawdown=-0.51,
        holdings=(FundHolding("300502", "新易盛", 6.0),
                  FundHolding("300308", "中际旭创", 5.5),
                  FundHolding("300408", "三环集团", 4.7),
                  FundHolding("688498", "源杰科技", 4.6),
                  FundHolding("600183", "生益科技", 4.5),
                  FundHolding("688256", "寒武纪", 4.1),
                  FundHolding("688012", "中微公司", 3.4),
                  FundHolding("301377", "鼎泰高科", 3.2),
                  FundHolding("00148", "建滔集团", 3.1),
                  FundHolding("01347", "华虹宏力", 2.8)),
        manager="郑希", manager_tenure="13年又328天",
    )
    base.update(overrides)
    return FundSnapshot(**base)


def _flat_months(n, r):
    """n monthly returns, all equal to r."""
    return tuple(r for _ in range(n))


def _wavy(n, base=0.0, amp=0.01):
    """Alternating monthly returns — a benchmark with estimable variance."""
    return tuple(base + (amp if i % 2 else -amp) for i in range(n))


def test_bullish_on_strong_uptrend():
    # +2%/mo for 36 months: momentum positive, calmar good, alpha positive
    # vs a flat benchmark.
    months = _flat_months(36, 0.02)
    bench = _flat_months(36, 0.0)
    result = FundQuantModel().score(_snap(), months, bench)
    assert result.signal == "bullish"
    assert result.confidence >= 50
    assert result.total > 0


def test_bearish_on_downtrend():
    months = _flat_months(36, -0.02)
    bench = _flat_months(36, 0.0)
    result = FundQuantModel().score(_snap(), months, bench)
    assert result.signal == "bearish"
    assert result.total < 0


def test_abstains_without_history():
    months = _flat_months(6, 0.02)
    result = FundQuantModel().score(_snap(), months, _flat_months(6, 0.0))
    assert result.signal == "neutral"
    assert result.confidence == 0.0
    assert "弃权" in result.reasoning


def test_drawdown_penalizes_calmar():
    # identical 12m return via different paths — smooth vs crash-recover.
    smooth = tuple(0.005 for _ in range(12))
    crash = (0.03,) * 6 + (-0.25,) + (0.03,) * 5
    s_smooth = FundQuantModel().score(_snap(), smooth, _flat_months(12, 0.0))
    s_crash = FundQuantModel().score(_snap(), crash, _flat_months(12, 0.0))
    assert s_smooth.components["calmar"] > s_crash.components["calmar"]


def test_concentration_penalizes_extremes():
    concentrated = _snap(holdings=(FundHolding("600519", "茅台", 30.0),
                                   FundHolding("000333", "美的", 35.0)))
    diluted = _snap(holdings=(FundHolding("600519", "茅台", 8.0),
                              FundHolding("000333", "美的", 7.0)))
    q = FundQuantModel()
    base = _flat_months(36, 0.01)
    bench = _flat_months(36, 0.0)
    assert q.score(concentrated, base, bench).components["concentration"] < 0
    assert q.score(diluted, base, bench).components["concentration"] > 0


def test_scale_and_fee_rules():
    q = FundQuantModel()
    base = _flat_months(36, 0.01)
    bench = _flat_months(36, 0.0)
    tiny = q.score(_snap(scale_billion=1.0), base, bench)
    huge = q.score(_snap(scale_billion=500.0), base, bench)
    pricey = q.score(_snap(purchase_fee=1.5, mgmt_fee=1.8, custody_fee=0.3),
                     base, bench)
    mid = q.score(_snap(scale_billion=30.0), base, bench)
    assert tiny.components["scale"] == -1.0
    assert huge.components["scale"] == -0.5
    assert pricey.components["fee"] == -0.5
    assert mid.components["scale"] == 0.0


def test_alpha_vs_benchmark():
    # fund tracks the benchmark exactly → alpha ≈ 0; fund lagging the
    # benchmark every month → negative alpha.
    bench = _wavy(36, amp=0.02)
    tracking = _wavy(36, amp=0.02)
    lagging = _wavy(36, base=-0.005, amp=0.02)
    q = FundQuantModel()
    assert abs(q.score(_snap(), tracking, bench).components["alpha"]) < 0.1
    assert q.score(_snap(), lagging, bench).components["alpha"] < -0.1


def test_momentum_rejects_last_month_reversal():
    # 12m strong (say +2%/mo for 12) but last month crashes → momentum cut.
    up = (0.02,) * 11 + (-0.30,)
    bench = _flat_months(12, 0.0)
    result = FundQuantModel().score(_snap(), up, bench)
    assert result.components["momentum"] < 0.5  # 12m gain muted by 1m crash
    # and the same 12 months WITHOUT the crash scores higher
    steady = _flat_months(12, 0.02)
    assert (FundQuantModel().score(_snap(), steady, bench)
            .components["momentum"] > result.components["momentum"])