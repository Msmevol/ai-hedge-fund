"""FundAnalyst — the LLM agent that reads a FundSnapshot and decides whether
the fund is worth buying.

This is deliberately NOT a stock-market LLMAgent subclass: the stock agents
reason over a FundamentalsSnapshot keyed by ticker/date through a DataClient.
Fund analysis is a different world (no earnings, no balance sheet), so the
analyst is a small standalone class with the same shape of outcome
(signal/confidence/reasoning) and the same fail-soft contract: data errors
abstain, LLM/parse errors abstain, unchanged snapshots reuse the cache.
"""

from __future__ import annotations

import json
import logging
import time

from hedge_fund.llm import LLMClient, PromptCache, extract_json, make_llm, prompt_key
from hedge_fund.fund.snapshot import FundSnapshot
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_SIGN_TO_LABEL = {
    "bullish": "推荐买入",
    "neutral": "可观望",
    "bearish": "不建议",
}
_SIGN_TO_MARK = {"bullish": "buy", "neutral": "hold", "bearish": "avoid"}


class FundVerdict(BaseModel):
    """The analyst's decision on one fund."""

    code: str
    name: str
    signal: str  # bullish / neutral / bearish
    confidence: float  # 0-100
    reasoning: str  # 中文理由
    rank_order: int = Field(default=0)  # 报告中的排序（0 = 推荐买入最优先）

    @property
    def label(self) -> str:
        return _SIGN_TO_LABEL.get(self.signal, self.signal)

    @property
    def mark(self) -> str:
        return _SIGN_TO_MARK.get(self.signal, "hold")

    @property
    def score(self) -> float:
        """Sort key: signal tier, then confidence."""
        return {"bullish": 300, "neutral": 200, "bearish": 100}[self.signal] + self.confidence


_SYSTEM_PROMPT = """你是资深公募基金研究员，精通中国公募基金（消费、医药、科技、新能源、红利、宽基指数、债券等主题）。

给你一只基金的基本面快照（规模、成立时间、费率、历史收益、最大回撤、前十大重仓股、基金经理），请判断：**现在该不该买这只基金**。

判断要点：
- 历史收益与回撤：收益是否稳健、回撤是否可接受，警惕高收益伴随过大回撤
- 规模与费率：规模过小（清盘风险）或费率偏高会扣分；货币化基金注意申购费
- 重仓股质量：重仓股是否质地优良、行业是否分散
- 基金经理：是否资深（从业年限长）、风格是否连贯
- 期限匹配：短钱买波动大的主题基金会扣分，资金期限长可以容忍回撤

必须输出严格 JSON（不要多余文字）：
{"signal": "bullish" | "neutral" | "bearish", "confidence": 0-100 的整数, "reasoning": "中文理由，60-150字，分点列出核心依据与风险"}
其中 bullish = 推荐买入，neutral = 可观望（有亮点也有明显风险），bearish = 不建议买入。

快照中标记"数据缺失"的字段直接忽略，不要臆测。"""


class FundAnalyst:
    """One verdict per fund snapshot; cheap (cached), safe (abstains)."""

    name = "fund_analyst"

    def __init__(self, llm: LLMClient | None = None,
                 cache: PromptCache | None = None) -> None:
        self._llm = llm if llm is not None else make_llm()
        self._cache = cache if cache is not None else PromptCache()

    def analyze(self, snapshot: FundSnapshot) -> FundVerdict:
        user = snapshot.render()
        key = prompt_key(self.name, self._llm.model, _SYSTEM_PROMPT, user)

        cached = self._cache.get(key)
        if cached is not None and "parsed" in cached:
            return FundVerdict(**cached["parsed"], code=snapshot.code,
                               name=snapshot.name)

        try:
            response = self._llm.complete(_SYSTEM_PROMPT, user)
        except Exception as exc:
            # Rate limits and 5xx are transient — one backoff retry before we
            # abstain (a 10-fund pool can trip a parallel quota).
            if not _transient(exc):
                logger.warning("基金分析 LLM 调用失败 %s: %s", snapshot.code, exc)
                return self._abstain(snapshot, f"LLM 调用失败: {exc}")
            time.sleep(2.0)
            try:
                response = self._llm.complete(_SYSTEM_PROMPT, user)
            except Exception as exc2:
                logger.warning("基金分析 LLM 调用失败(重试后) %s: %s", snapshot.code, exc2)
                return self._abstain(snapshot, f"LLM 调用失败: {exc2}")

        record = {
            "agent": self.name,
            "model": self._llm.model,
            "code": snapshot.code,
            "snapshot_hash": snapshot.content_hash,
            "system": _SYSTEM_PROMPT,
            "user": user,
            "response": response,
        }
        try:
            parsed = self._parse(response)
        except Exception as exc:
            self._cache.put(key, {**record, "parse_error": str(exc)})
            logger.warning("基金分析解析失败 %s: %s", snapshot.code, exc)
            return self._abstain(snapshot, f"解析失败: {exc}")

        self._cache.put(key, {**record, "parsed": parsed})
        return FundVerdict(**parsed, code=snapshot.code, name=snapshot.name)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _parse(self, response: str) -> dict:
        data = extract_json(response)
        signal = str(data.get("signal", "")).lower()
        if signal not in _SIGN_TO_LABEL:
            raise ValueError(f"无效信号 {data.get('signal')!r}")
        confidence = float(data.get("confidence", 0))
        if not 0 <= confidence <= 100:
            raise ValueError(f"置信度越界: {confidence}")
        return {
            "signal": signal,
            "confidence": confidence,
            "reasoning": str(data.get("reasoning", "")),
        }

    def _abstain(self, snapshot: FundSnapshot, reason: str) -> FundVerdict:
        return FundVerdict(code=snapshot.code, name=snapshot.name,
                           signal="neutral", confidence=0.0,
                           reasoning=f"无法分析（{reason}）")


def _sort_verdicts(verdicts: list[FundVerdict]) -> list[FundVerdict]:
    """Rank: bullish first, then neutral, then bearish; by confidence within
    a tier. Used by the picker screen; kept here as a pure, testable
    function."""
    ordered = sorted(verdicts, key=lambda v: v.score, reverse=True)
    for rank, verdict in enumerate(ordered, start=1):
        verdict.rank_order = rank
    return ordered


def _transient(exc: Exception) -> bool:
    """Rate-limit / server errors deserve a retry; everything else fails
    fast. OpenAI-style clients raise rich `APIStatusError`s."""
    name = type(exc).__name__.upper()
    if "APISTATUSERROR" in name or "OPENAI" in name:
        status = getattr(exc, "status_code", None)
        if status in (403, 429, 500, 502, 503, 504):
            return True
    return False