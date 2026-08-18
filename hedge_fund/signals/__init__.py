"""Alpha models — view-forming components of the quant stack.

See hedge_fund/signals/base.py for the AlphaModel / QuantModel interface.
Concrete models register here as they are implemented. Two flavors, one
interface: LLM investor agents (persona system prompts on LLMAgent) and
quant models (pure math).
"""

from __future__ import annotations

from hedge_fund.signals.base import AlphaModel, QuantModel
from hedge_fund.signals.buffett import BuffettAgent
from hedge_fund.signals.druckenmiller import DruckenmillerAgent
from hedge_fund.signals.graham import GrahamAgent
from hedge_fund.signals.llm_agent import LLMAgent
from hedge_fund.signals.lynch import LynchAgent
from hedge_fund.signals.munger import MungerAgent
from hedge_fund.signals.pead import PEADModel

ALPHA_MODEL_REGISTRY: dict[str, type[AlphaModel]] = {
    # Quant models
    "pead": PEADModel,
    # LLM investor agents
    "buffett": BuffettAgent,
    "munger": MungerAgent,
    "graham": GrahamAgent,
    "lynch": LynchAgent,
    "druckenmiller": DruckenmillerAgent,
}

# Imported LAST: fund_analyst/fund_quant pull in hedge_fund.fund, whose spec
# module imports ALPHA_MODEL_REGISTRY from this package — a mid-file import
# here would hit the registry mid-definition (circular).
from hedge_fund.signals.fund_analyst import (  # noqa: E402
    FundAnalyst,
    FundVerdict,
    _sort_verdicts,
)
from hedge_fund.signals.fund_quant import FundQuantModel, FundQuantResult  # noqa: E402

__all__ = [
    "AlphaModel",
    "QuantModel",
    "LLMAgent",
    "BuffettAgent",
    "MungerAgent",
    "GrahamAgent",
    "LynchAgent",
    "DruckenmillerAgent",
    "FundAnalyst",
    "FundQuantModel",
    "FundQuantResult",
    "FundVerdict",
    "PEADModel",
    "_sort_verdicts",
    "ALPHA_MODEL_REGISTRY",
]
