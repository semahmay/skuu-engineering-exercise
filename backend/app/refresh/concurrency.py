"""Shared, bounded product-level concurrency policy for the refresh pipeline.

Two independent ceilings combine:

* a **per-shop** limit — how many product tasks one website may run at once
  (hard-capped at :data:`MAX_PRODUCT_CONCURRENCY` = 5), and
* one **global** limit — how many product tasks may run across the *whole*
  refresh at once, shared by every shop through a single semaphore held by
  :class:`RefreshConcurrency`.

The global semaphore is created once per refresh run and handed to each scraper,
so per-shop limits can never multiply into an unbounded number of in-flight
tasks. Shops are deliberately still scheduled **sequentially** (that keeps the
dashboard's single "current shop" progress model deterministic and avoids
launching several browsers at once), so in practice the effective limit is the
current shop's limit; the shared gate is what guarantees the global ceiling if
shops are ever overlapped.

Evidence behind the defaults (see the task report):

* ``carhartt_wip`` = 3 — measured safe and correct over a fixed 15-URL sample.
* HTTP/``products.json`` shops = 3 — their per-product task performs **no extra
  request to the retailer** (the catalogue is already in memory); concurrency
  only overlaps *independent local database upserts* on distinct
  ``(shop, url, size)`` keys, so it cannot increase retailer load.
* ``veja`` / ``ssense`` = 1 — Playwright per-product navigation, not yet
  measured and (SSENSE) bot-gated, so they stay sequential.

Everything is overridable per shop via :data:`SHOP_PRODUCT_CONCURRENCY` and
globally via the ``SKUU_GLOBAL_PRODUCT_CONCURRENCY`` environment variable.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Hard per-shop ceiling (product tasks in flight for one website).
MAX_PRODUCT_CONCURRENCY = 5
# Conservative default for any shop not explicitly configured.
DEFAULT_PRODUCT_CONCURRENCY = 1
# Global ceiling for the whole refresh (all shops together).
GLOBAL_PRODUCT_LIMIT_DEFAULT = 6


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


GLOBAL_PRODUCT_LIMIT = max(1, _env_int("SKUU_GLOBAL_PRODUCT_CONCURRENCY", GLOBAL_PRODUCT_LIMIT_DEFAULT))


# Per-shop safe product concurrency. Anything absent falls back to
# DEFAULT_PRODUCT_CONCURRENCY (sequential).
SHOP_PRODUCT_CONCURRENCY: Dict[str, int] = {
    # Playwright per-product navigation, measured safe/correct on a fixed sample.
    "carhartt_wip": 3,
    # HTTP ``products.json`` catalogue shops: the per-product task is a local
    # database upsert (no per-product HTTP request), so overlapping independent
    # upserts is safe and does not touch the retailer any harder.
    "asphaltgold": 3,
    "salomon": 3,
    "afew": 3,
    "beamhill": 3,
    "biffi": 3,
    "caliroots": 3,
    "footway": 3,
    "nugnes1920": 3,
    "overkillshop": 3,
    "sneakersnstuff": 3,
    "xtreme": 3,
    # Playwright per-product navigation, unmeasured / bot-gated: stay sequential.
    "veja": 1,
    "ssense": 1,
}

# Shops whose scraper has no bounded-concurrency implementation yet. They always
# run sequentially regardless of the configured limit.
SEQUENTIAL_ONLY_SHOPS = frozenset({"veja", "ssense"})


def clamp_limit(value: Any) -> int:
    """Coerce ``value`` into ``[1, MAX_PRODUCT_CONCURRENCY]``."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_PRODUCT_CONCURRENCY
    return max(1, min(parsed, MAX_PRODUCT_CONCURRENCY))


def shop_limit(shop_id: str) -> int:
    """Configured, clamped per-shop product concurrency."""
    return clamp_limit(SHOP_PRODUCT_CONCURRENCY.get(shop_id, DEFAULT_PRODUCT_CONCURRENCY))


class RefreshConcurrency:
    """The single global product-task gate shared by every shop in one run.

    Only one instance is created per refresh (:func:`app.refresh.pipeline.run_refresh`),
    so the global ceiling cannot be multiplied by instantiating it per shop.
    """

    def __init__(self, global_limit: Optional[int] = None) -> None:
        self.global_limit = max(1, int(global_limit) if global_limit else GLOBAL_PRODUCT_LIMIT)
        self._gate = asyncio.Semaphore(self.global_limit)
        self.active = 0
        self.peak = 0
        self._count_lock = asyncio.Lock()

    def limit_for(self, shop_id: str) -> int:
        """Effective per-shop limit: never above the global ceiling."""
        if shop_id in SEQUENTIAL_ONLY_SHOPS:
            return 1
        return min(shop_limit(shop_id), self.global_limit)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one global product slot (and track the live peak)."""
        async with self._gate:
            async with self._count_lock:
                self.active += 1
                if self.active > self.peak:
                    self.peak = self.active
            try:
                yield
            finally:
                async with self._count_lock:
                    self.active -= 1

    def snapshot(self) -> Dict[str, int]:
        return {"global_limit": self.global_limit, "active": self.active, "peak": self.peak}


async def run_bounded(
    items: List[Any],
    worker,
    *,
    limit: int,
    rc: Optional[RefreshConcurrency] = None,
) -> List[Optional[Tuple[bool, Any]]]:
    """Run ``worker(item)`` for every item with strictly bounded concurrency.

    * A fixed pool of at most ``limit`` worker coroutines is created (never one
      task per item), so a 30 000-product catalogue cannot spawn 30 000 tasks.
    * When ``rc`` is given, each in-flight item also holds one global slot, so the
      whole refresh stays under ``rc.global_limit``.
    * A worker raising does not cancel the others: its slot is stored as
      ``(False, exception)`` and the remaining items still run.

    Returns a list parallel to ``items`` of ``(ok, value_or_exception)``.
    """
    materialised = list(items)
    n = len(materialised)
    outcomes: List[Optional[Tuple[bool, Any]]] = [None] * n
    if n == 0:
        return outcomes

    pool_size = min(max(1, int(limit)), n)
    cursor = {"i": 0}
    cursor_lock = asyncio.Lock()

    async def consume() -> None:
        while True:
            async with cursor_lock:
                index = cursor["i"]
                cursor["i"] += 1
            if index >= n:
                return

            async def _call() -> Tuple[bool, Any]:
                try:
                    return True, await worker(materialised[index])
                except Exception as exc:  # noqa: BLE001 - isolated per item
                    return False, exc

            if rc is None:
                outcomes[index] = await _call()
            else:
                async with rc.slot():
                    outcomes[index] = await _call()

    await asyncio.gather(*(consume() for _ in range(pool_size)))
    return outcomes
