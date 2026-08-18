"""FundAnalyst tests — fake LLM + real FundSnapshot, no network."""

import json

import pytest

from hedge_fund.fund import FundHolding, FundSnapshot
from hedge_fund.llm import PromptCache
from hedge_fund.signals import FundAnalyst, _sort_verdicts


class FakeLLM:
    model = "fake-model"

    def __init__(self, response="", error=None):
        self._response = response
        self._error = error
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


def _snap():
    return FundSnapshot(
        code="110022", name="易方达消费行业股票", fund_type="股票型",
        inception="2010-08-20", scale_billion=34.0,
        purchase_fee=1.5, mgmt_fee=1.2, custody_fee=0.2,
        return_1y=-0.07, return_3y=-0.22, return_5y=None, ytd=-0.03,
        max_drawdown=-0.68,
        holdings=(FundHolding("600519", "贵州茅台", 9.77),),
        manager="王元春", manager_tenure="13年327天",
    )


_BULLISH = json.dumps({"signal": "bullish", "confidence": 82,
                       "reasoning": "龙头重仓，规模可观，费率合理。"})


def _analyst(tmp_path, llm):
    return FundAnalyst(llm=llm, cache=PromptCache(tmp_path / "llmcache"))


def test_analyze_parses_verdict(tmp_path):
    llm = FakeLLM(_BULLISH)
    v = _analyst(tmp_path, llm).analyze(_snap())
    assert v.signal == "bullish"
    assert v.confidence == 82
    assert "龙头重仓" in v.reasoning
    assert v.code == "110022"
    assert v.label == "推荐买入"
    assert v.mark == "buy"
    assert llm.calls == 1


def test_analyze_caches_on_identical_snapshot(tmp_path):
    llm = FakeLLM(_BULLISH)
    a = _analyst(tmp_path, llm)
    snap = _snap()
    a.analyze(snap)
    a.analyze(snap)  # unchanged content_hash → no second LLM call
    assert llm.calls == 1


def test_analyze_abstains_on_llm_error(tmp_path):
    llm = FakeLLM(error=RuntimeError("boom"))
    v = _analyst(tmp_path, llm).analyze(_snap())
    assert v.signal == "neutral"
    assert v.confidence == 0
    assert "无法分析" in v.reasoning


def test_analyze_abstains_on_garbage_response(tmp_path):
    llm = FakeLLM("not json at all")
    v = _analyst(tmp_path, llm).analyze(_snap())
    assert v.signal == "neutral"
    assert v.confidence == 0
    assert "无法分析" in v.reasoning


@pytest.mark.parametrize("bad", [
    json.dumps({"signal": "crazy", "confidence": 50, "reasoning": "x"}),
    json.dumps({"signal": "bullish", "confidence": 999, "reasoning": "x"}),
])
def test_analyze_rejects_invalid_verdict(tmp_path, bad):
    v = _analyst(tmp_path, FakeLLM(bad)).analyze(_snap())
    assert v.signal == "neutral" and v.confidence == 0


def test_sort_verdicts_orders_by_signal_then_confidence():
    from hedge_fund.signals import FundVerdict

    v1 = FundVerdict(code="1", name="a", signal="bearish", confidence=99,
                     reasoning="r")
    v2 = FundVerdict(code="2", name="b", signal="bullish", confidence=60,
                     reasoning="r")
    v3 = FundVerdict(code="3", name="c", signal="neutral", confidence=95,
                     reasoning="r")
    v4 = FundVerdict(code="4", name="d", signal="bullish", confidence=90,
                     reasoning="r")
    ranked = _sort_verdicts([v1, v2, v3, v4])
    assert [v.code for v in ranked] == ["4", "2", "3", "1"]
    assert [v.rank_order for v in ranked] == [1, 2, 3, 4]


def test_score_tiers():
    from hedge_fund.signals import FundVerdict

    hold = FundVerdict(code="1", name="a", signal="neutral", confidence=99,
                       reasoning="r")
    buy = FundVerdict(code="2", name="b", signal="bullish", confidence=1,
                      reasoning="r")
    assert buy.score > hold.score  # tier beats confidence