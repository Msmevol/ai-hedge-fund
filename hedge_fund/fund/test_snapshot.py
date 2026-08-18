"""FundSnapshot tests — data class contract, no network."""

from hedge_fund.fund import FundHolding, FundSnapshot


def _snap(**overrides):
    base = dict(
        code="519714", name="交银消费新驱动股票", fund_type="股票型",
        inception="2012-11-07", scale_billion=38.0,
        purchase_fee=1.5, mgmt_fee=1.2, custody_fee=0.2,
        return_1y=-0.071, return_3y=-0.22, return_5y=0.31, ytd=-0.03,
        max_drawdown=-0.68,
        holdings=(FundHolding("600519", "贵州茅台", 9.03),),
        manager="韩威俊", manager_tenure="10年又213天",
    )
    base.update(overrides)
    return FundSnapshot(**base)


def test_render_covers_all_sections():
    text = _snap().render()
    assert "519714" in text and "交银消费新驱动股票" in text
    assert "近1年 -7.1%" in text
    assert "近3年 -22.0%" in text
    assert "最大回撤 -68.0%" in text
    assert "贵州茅台 9.0%" in text
    assert "申购 1.50% | 管理 1.20% | 托管 0.20%" in text
    assert "韩威俊" in text
    assert "数据缺失" not in text  # nothing missing here


def test_render_treats_missing_fields_as_数据缺失():
    text = _snap(scale_billion=None, manager=None,
                 holdings=()).render()
    assert "数据缺失" in text
    assert "基金经理" not in text


def test_render_signs_positive_returns():
    text = _snap(return_1y=0.12).render()
    assert "近1年 +12.0%" in text


def test_content_hash_stable_and_sensitive():
    a = _snap()
    b = _snap()
    assert a.content_hash == b.content_hash
    c = _snap(scale_billion=100.0)
    assert a.content_hash != c.content_hash


def test_content_hash_ignores_nothing_the_analyst_sees():
    a = _snap()
    b = _snap(manager_tenure="1年")
    assert a.content_hash != b.content_hash