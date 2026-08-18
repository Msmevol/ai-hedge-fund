"""FundClient tests — pure parsers against canned feed bodies, and the pool
flow with the HTTP layer stubbed. No network."""

import pytest

from hedge_fund.data.fund_client import (
    FundClient,
    FundInfo,
    _max_drawdown,
    _parse_allocation,
    _parse_fees,
    _parse_holdings,
    _parse_manager,
    _parse_pz_vars,
    _rank_row,
    _window_returns,
)

# ---------------------------------------------------------------------------
# Canned feed bodies (shapes copied from live responses)
# ---------------------------------------------------------------------------

PZ_JS = """
var fS_name = "易方达消费行业股票";var fS_code = "110022";
var fund_sourceRate="1.50";var fund_Rate="0.15";
var syl_1n="-16.35";var syl_1y="2.99";var syl_3y="-0.81";var syl_6y="-12.91";
var Data_fundSharesPositions = [[1784476800000,86.9800]];
var Data_netWorthTrend = [
{"x":1282233600000,"y":1.0,"equityReturn":0,"unitMoney":""},
{"x":1282838400000,"y":0.9,"equityReturn":-10,"unitMoney":""},
{"x":1283443200000,"y":0.99,"equityReturn":10,"unitMoney":""}];
var Data_ACWorthTrend = [[1282233600000,1.0],[1282838400000,0.9],[1283443200000,0.99]];
var Data_assetAllocation = {"series":[
{"name":"股票占比","data":[92.52,90.85]},
{"name":"总资产","data":[169.492,150.0094]}],"categories":["2025-09-30","2025-12-31"]};
var Data_currentFundManager = [{"id":"30189741","name":"王元春","star":4,"workTime":"13年327天"}];
var Data_buySedemption = {"series":[],"categories":[]};
"""

JJCC_HTML = """
<div class='box'><h4>2026年2季度股票投资明细</h4>
<table class='comm tzxq'><thead><tr><th>序号</th><th>股票代码</th><th>股票名称</th><th>最新价</th><th>涨跌幅</th><th>相关资讯</th><th>占净值比例</th><th>持股数</th><th>持仓市值</th></tr></thead>
<tbody>
<tr><td>1</td><td><a href='//quote.eastmoney.com/unify/r/1.600519'>600519</a></td><td class='tol'><a href='//quote.eastmoney.com/unify/r/1.600519'>贵州茅台</a></td><td class='tor'><span data-id='dq600519'></span></td><td class='tor'><span data-id='zd600519'></span></td><td class='xglj'><a>变动详情</a></td><td class='tor'>9.77%</td><td class='tor'>80.00</td><td class='tor'>94,838.96</td></tr>
<tr><td>2</td><td><a>000333</a></td><td class='tol'><a>美的集团</a></td><td class='tor'>55.50</td><td class='tor'>1.02</td><td class='xglj'></td><td class='tor'>9.31%</td><td class='tor'>1,196.52</td><td class='tor'>90,372.87</td></tr>
</tbody></table></div>
<div class='box'><h4>2025年4季度股票投资明细</h4>
<table class='comm tzxq'><tbody>
<tr><td>1</td><td><a>600519</a></td><td class='tol'><a>贵州茅台</a></td><td class='tor'></td><td class='tor'></td><td class='xglj'></td><td class='tor'>9.39%</td><td class='tor'>83.00</td><td class='tor'>95,838.96</td></tr>
</tbody></table></div>
"""

JJFL_HTML = """
<table class="w770 comm jjfl"><tbody>
<tr><td class="th w110">管理费率</td><td class="w135">1.20%（每年）</td><td class="th w110">托管费率</td><td class="w135">0.20%（每年）</td><td class="th w110">销售服务费率</td><td class="w135">---</td></tr>
</tbody></table>
"""

RANK_JS = """
var rankData = {datas:["519714,交银消费新驱动股票,JYXFQD,2026-08-18,4.0,4.0,1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0,1.2,180.0,2012-11-07,1,180.0,1.50%,0.15%,1,0.15%,1,38.0","110022,易方达消费行业股票,YFXDX,2026-08-18,3.0,3.0,1.0,2.0,3.0,4.0,5.0,6.0,7.0,8.0,1.5,340.0,2010-08-20,1,340.0,1.50%,0.15%,1,0.15%,1,34.21"],allRecords:4487,pageIndex:1,pageNum:5,allPages:898,allNum:20133,zs_count:4487};
"""


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------

def test_parse_pz_vars_extracts_js_variables():
    v = _parse_pz_vars(PZ_JS)
    assert v["fS_name"] == "易方达消费行业股票"
    assert v["fund_sourceRate"] == "1.50"
    assert v["Data_netWorthTrend"][0]["y"] == 1.0
    assert v["Data_ACWorthTrend"][0] == [1282233600000, 1.0]
    assert v["Data_assetAllocation"]["series"][1]["name"] == "总资产"


def test_parse_holdings_takes_newest_quarter_only():
    hs = _parse_holdings(JJCC_HTML)
    assert [h.name for h in hs] == ["贵州茅台", "美的集团"]  # not the Q2 row
    assert hs[0].percent == pytest.approx(9.77)
    assert hs[1].percent == pytest.approx(9.31)


def test_parse_holdings_tolerates_quote_cells():
    # Row 2 has live quote numbers ("55.50", "1.02") — they must not leak
    # into the NAV ratio.
    hs = _parse_holdings(JJCC_HTML)
    assert hs[1].percent == pytest.approx(9.31)


def test_parse_fees():
    assert _parse_fees(JJFL_HTML) == (1.2, 0.2)


def test_parse_allocation_latest_total_asset():
    v = _parse_pz_vars(PZ_JS)
    assert _parse_allocation(v["Data_assetAllocation"]) == pytest.approx(150.0094)


def test_parse_manager():
    v = _parse_pz_vars(PZ_JS)
    assert _parse_manager(v["Data_currentFundManager"]) == ("王元春", "13年327天")
    assert _parse_manager(None) is None
    assert _parse_manager([]) is None


def _m(y, m, d):
    """Day-aligned epoch milliseconds (no platform %s)."""
    from datetime import date as dt

    return int((dt(y, m, d) - dt(1970, 1, 1)).total_seconds()) * 1000


def test_window_returns_point_to_point():
    acw = [[0, 1.0], [1, 1.1], [2, 0.99], [3, 1.1]]
    # 365-day window needs history beyond the series → None everywhere.
    a, b, c, ytd = _window_returns(acw)
    assert a is None and b is None and c is None


def test_window_returns_ytd():
    acw = [[_m(2025, 12, 31), 1.0],
           [_m(2026, 1, 3), 1.05],
           [_m(2026, 8, 18), 1.1]]
    a, b, c, ytd = _window_returns(acw)
    assert a is None  # not a full year of history
    assert ytd == pytest.approx(0.1)


def test_max_drawdown_handles_dict_rows():
    nav = [{"x": 1, "y": 1.0}, {"x": 2, "y": 0.8}, {"x": 3, "y": 0.95}]
    assert _max_drawdown(nav) == pytest.approx(-0.2)


def test_max_drawdown_pair_rows_and_empty():
    assert _max_drawdown([[1, 1.0], [2, 0.5], [3, 0.6]]) == pytest.approx(-0.5)
    assert _max_drawdown([]) is None
    assert _max_drawdown([[1, 1.0], [2, 1.1]]) is None  # no drawdown


def test_rank_row_pads_short_rows():
    row = _rank_row("000001,华夏成长,HC,2012-01-01,1.0,1.0")
    assert row[0] == "000001" and len(row) == 25
    assert row[24] == ""


# ---------------------------------------------------------------------------
# Pool flow (HTTP stubbed)
# ---------------------------------------------------------------------------

class _FakeClient(FundClient):
    """list_funds with the page fetch replaced by canned rows."""

    def __init__(self, pages):
        super().__init__()
        self._pages = pages
        self.calls = []

    def _pool_page(self, ft, page):
        self.calls.append((ft, page))
        return self._pages.pop(0) if self._pages else []


def _rows():
    import re

    text = RANK_JS
    body = re.search(r"datas:\[(.*?)\],allRecords", text, re.S).group(1)
    return [_rank_row(csv_data) for csv_data in re.findall(r'"([^"]*)"', body)]


def test_list_funds_filters_and_sorts():
    client = _FakeClient([_rows()])
    pool = client.list_funds("消费", limit=1)
    assert len(pool) == 1
    assert pool[0].code == "519714"  # scale 38.0 > 34.21
    assert pool[0].scale_billion == 38.0
    assert pool[0].purchase_fee == 1.5
    assert pool[0].fund_type == "股票型"
    assert client.calls[0][0] == "gp"  # 消费 → gp first (then hh)


def test_list_funds_rejects_unknown_theme():
    with pytest.raises(ValueError):
        FundClient().list_funds("不存在的主题")


def test_list_funds_empty_pool():
    client = _FakeClient([])
    assert client.list_funds("消费") == []