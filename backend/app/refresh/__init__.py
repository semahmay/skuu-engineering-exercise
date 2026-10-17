"""The "Refresh Inventory" pipeline: scrape -> upsert -> match -> restock -> metrics.

See :mod:`app.refresh.pipeline` for orchestration and
:mod:`app.refresh.manager` for the single-job lifecycle.
"""

from app.refresh.concurrency import (
    MAX_PRODUCT_CONCURRENCY,
    RefreshConcurrency,
    shop_limit,
)
from app.refresh.job import RefreshJob, ShopResult
from app.refresh.manager import (
    NoActiveRefresh,
    RefreshError,
    RefreshInProgress,
    RefreshManager,
    manager,
)
from app.refresh.pipeline import RefreshCancelled, run_refresh
from app.refresh.shops import SHOPS, ShopSpec

__all__ = [
    "RefreshJob",
    "ShopResult",
    "RefreshManager",
    "RefreshError",
    "RefreshInProgress",
    "NoActiveRefresh",
    "RefreshCancelled",
    "run_refresh",
    "manager",
    "SHOPS",
    "ShopSpec",
    "RefreshConcurrency",
    "MAX_PRODUCT_CONCURRENCY",
    "shop_limit",
]
