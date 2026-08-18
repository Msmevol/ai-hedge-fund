"""v2 data pipeline — data provider protocol, FD client, and response models.

Note: the 天天基金 client (hedge_fund/data/fund_client.py) is NOT re-exported
here — importing it pulls in hedge_fund.fund (its snapshot types), which
imports hedge_fund.signals (spec → ALPHA_MODEL_REGISTRY). Re-exporting it
from this package would make the stock pipeline pay for that cycle. Import
`from hedge_fund.data.fund_client import FundClient` directly instead.
"""

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient, FDClientError
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    Filing,
    FinancialMetrics,
    InsiderTrade,
    Price,
)
from hedge_fund.data.protocol import DataClient

__all__ = [
    "CachedDataClient",
    "CompanyFacts",
    "CompanyNews",
    "DataClient",
    "Earnings",
    "EarningsData",
    "EarningsRecord",
    "FDClient",
    "FDClientError",
    "Filing",
    "FinancialMetrics",
    "InsiderTrade",
    "Price",
]