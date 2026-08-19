"""Shared analysis pipeline tests — canned data, no network, no LLM."""

import pytest

from hedge_fund.data.fund_client import FundInfo, QuantInput
from hedge_fund.fund import FundHolding, FundSnapshot
from hedge_fund.signals import FundQuantResult, FundVerdict
from hedge_fund.web import analysis


def _snap(code="010013", name="科技基金A"):
    return FundSnapshot(
        code=code, name=name, fund_type="股票型", inception="2020-08-24",
        scale_billion=152.0, purchase_fee=1.5, mgmt_fee=1.2, custody_fee=0.2,
        return_1y=0.5, return_3y=1.1, return_5y=None, ytd=0.2,
        max_drawdown=-0.3,
        holdings=(FundHolding("300502", "新易盛", 6.0),),
        manager="郑希", manager_tenure="13年")


class _FakeClient:
    def __init__(self, infos, snaps):
        self._infos = infos
        self._snaps = snaps

    def list_funds(self, theme):
        return self._infos

    def fetch_snapshot(self, info):
        return self._snaps[info.code]

    def fetch_quant_input(self, code):
        return QuantInput(fund_monthly=(0.01,) * 24, bench_monthly=(0.0,) * 24)

    def close(self):
        pass


class _FakeQuant:
    def score(self, snapshot, fund_monthly, bench_monthly):
        return FundQuantResult(signal="bullish", confidence=80.0, total=0.5,
                               components={"momentum": 0.5},
                               raw={"momentum_12m": 0.3})


class _FakeAnalyst:
    def __init__(self, signals):
        self._signals = signals

    def analyze(self, snapshot):
        sig = self._signals.get(snapshot.code, "neutral")
        return FundVerdict(code=snapshot.code, name=snapshot.name,
                           signal=sig, confidence=60.0, reasoning="理由")


@pytest.fixture
def env(monkeypatch):
    infos = [
        FundInfo(code="010013", name="科技基金A", fund_type="股票型",
                 inception="2020-08-24", scale_billion=152.0,
                 purchase_fee=1.5),
        FundInfo(code="010236", name="科技基金B", fund_type="股票型",
                 inception="2020-09-24", scale_billion=134.0,
                 purchase_fee=None),
    ]
    snaps = {i.code: _snap(i.code, i.name) for i in infos}
    monkeypatch.setattr(analysis, "FundClient",
                        lambda: _FakeClient(infos, snaps))
    monkeypatch.setattr(analysis, "FundQuantModel", _FakeQuant)
    monkeypatch.setattr(analysis, "FundAnalyst",
                        lambda: _FakeAnalyst({"010013": "bullish",
                                              "010236": "bearish"}))
    return infos, snaps


def test_event_sequence(env):
    events = []
    analysis.run_fund_analysis("科技", events.append)
    kinds = [e["type"] for e in events]
    assert kinds == ["pool", "fund_done", "fund_done", "done"]
    pool = events[0]
    assert pool["count"] == 2 and pool["theme"] == "科技"
    assert {f["code"] for f in pool["funds"]} == {"010013", "010236"}
    done_events = [e for e in events if e["type"] == "fund_done"]
    for ev in done_events:
        assert ev["signal"] in ("bullish", "neutral", "bearish")
        assert ev["snapshot"]["code"] == ev["code"]
        assert ev["quant"]["total"] == 0.5
    # both funds' snapshots survive the dict round-trip
    assert {e["snapshot"]["code"] for e in done_events} == {"010013", "010236"}
    assert done_events[0]["snapshot"]["holdings"][0]["name"] == "新易盛"


def test_done_order_bullish_first(env):
    events = []
    analysis.run_fund_analysis("科技", events.append)
    order = events[-1]["order"]
    # bullish (010013) ranks ahead of bearish (010236) regardless of order
    assert order[0]["code"] == "010013"
    assert order[0]["signal"] == "bullish"
    assert order[0]["rank"] == 1
    assert order[0]["label"] == "推荐买入"
    assert order[1]["code"] == "010236"


def test_per_fund_failure_abstains(env, monkeypatch):
    infos, snaps = env
    def boom(info):
        raise ConnectionError("网络错误")
    monkeypatch.setattr(analysis, "FundClient",
                        lambda: _FakeClient(infos, snaps))
    # make every _one fail by breaking fetch_snapshot
    class _BrokenClient(_FakeClient):
        def fetch_snapshot(self, info):
            raise ConnectionError("网络错误")
    monkeypatch.setattr(analysis, "FundClient", lambda: _BrokenClient(infos, snaps))
    events = []
    analysis.run_fund_analysis("科技", events.append)
    done_events = [e for e in events if e["type"] == "fund_done"]
    assert len(done_events) == 2
    for ev in done_events:
        assert ev["signal"] == "neutral"
        assert ev["confidence"] == 0.0
        assert "无法分析" in ev["reasoning"]
    assert events[-1]["type"] == "done"


def test_pool_failure_raises(env, monkeypatch):
    infos, snaps = env
    class _EmptyClient(_FakeClient):
        def list_funds(self, theme):
            return []
    monkeypatch.setattr(analysis, "FundClient", lambda: _EmptyClient(infos, snaps))
    with pytest.raises(ValueError, match="没有符合条件"):
        analysis.run_fund_analysis("科技", lambda e: None)


def test_snapshot_round_trip():
    snap = _snap()
    assert analysis._snapshot_from_dict(analysis._snapshot_dict(snap)) == snap


def test_fuse_rules():
    def v(sig, conf, reason="理由"):
        return FundVerdict(code="010013", name="科技基金A",
                           signal=sig, confidence=conf, reasoning=reason)
    def q(sig, conf, total=0.0):
        return FundQuantResult(signal=sig, confidence=conf, total=total)
    # same direction → stronger confidence
    same = analysis.fuse(v("bullish", 60), q("bullish", 90))
    assert same.signal == "bullish" and same.confidence == 90.0
    # direct clash → neutral
    clash = analysis.fuse(v("bullish", 60), q("bearish", 50))
    assert clash.signal == "neutral"
    assert clash.confidence == 55.0
    # quant abstains → LLM stands
    assert analysis.fuse(v("bearish", 70), q("neutral", 0)).signal == "bearish"
    # LLM failed → quant stands
    llm_fail = v("neutral", 0, "无法分析（LLM 调用失败）")
    assert analysis.fuse(llm_fail, q("bullish", 80)).signal == "bullish"
    # one side neutral → other side stands
    assert analysis.fuse(v("neutral", 60), q("bullish", 80)).signal == "bullish"