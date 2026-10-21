"""Database-backed entry points for the matching engine.

Loads SKU documents from MongoDB, normalizes them, runs the matcher and returns
the dashboard results. Read-only: nothing here mutates the collection.

The dashboard calls two endpoints (``/metrics`` and ``/matching/groups``) that
need the *same* data: every SKU document, normalized, then clustered. Building
that twice per page load doubled both the MongoDB scan and the CPU-bound
matcher. Instead both endpoints are fed by one shared, in-process snapshot that
is built once and reused until the SKU data changes.

Invalidation is explicit: :func:`invalidate_matching_cache` is called by the
refresh pipeline after each shop writes, and by the repository write helpers.
A short TTL is kept purely as a safety net in case a write path ever forgets to
notify; it is not the primary freshness mechanism.

The cache is per process. That matches the current deployment (a single
``uvicorn`` worker, because the refresh job state is also in-process): multiple
workers would each hold their own snapshot, still correct, just not shared.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from app.matching.matcher import (
    Cluster,
    Metrics,
    build_clusters,
    build_colour_vocabulary,
    compute_metrics,
    matched_group_samples,
    normalize_sku,
)

logger = logging.getLogger(__name__)

# Only the fields the matcher needs — keeps the scan light on large collections.
PROJECTION = {
    "_id": 0,
    "shop": 1,
    "brand": 1,
    "title": 1,
    "colour": 1,
    "size": 1,
    "in_stock": 1,
    "barcode": 1,
    "price": 1,
    "currency": 1,
    "url": 1,
}

# Safety net only: writes invalidate the cache explicitly. This bounds how long
# a snapshot can survive if some future write path forgets to call
# ``invalidate_matching_cache``.
CACHE_TTL_SECONDS = float(os.getenv("SKUU_MATCHING_CACHE_TTL_SECONDS", "300"))


@dataclass(frozen=True)
class MatchingSnapshot:
    """One consistent view of the SKU data: metrics and clusters together.

    Both dashboard endpoints read from the same instance, so the headline
    numbers and the detail list can never disagree (they are produced from the
    same cluster set).
    """

    metrics: Metrics
    clusters: List[Cluster]
    total_skus: int
    built_at: float  # wall-clock epoch seconds (for display)
    built_monotonic: float  # monotonic seconds (for cache age)
    load_ms: float
    normalize_ms: float
    cluster_ms: float
    metrics_ms: float

    @property
    def build_ms(self) -> float:
        return self.load_ms + self.normalize_ms + self.cluster_ms + self.metrics_ms

    def cache_info(self, *, hit: bool) -> Dict[str, Any]:
        return {
            "hit": hit,
            "age_seconds": round(max(0.0, time.monotonic() - self.built_monotonic), 3),
            "build_ms": round(self.build_ms, 1),
            "total_skus": self.total_skus,
        }


# ---------------------------------------------------------------------------
# Shared snapshot cache (process-wide)
# ---------------------------------------------------------------------------

_snapshot: Optional[MatchingSnapshot] = None
_version = 0  # bumped whenever SKU data changes
_built_version = -1  # version the current snapshot was built from
_lock: Optional[asyncio.Lock] = None
_lock_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_lock() -> asyncio.Lock:
    """Return a lock bound to the running loop.

    Tests create a fresh event loop per test; binding a single lock at import
    time would make the second test fail with "bound to a different event
    loop". Production keeps one loop, so this creates the lock exactly once.
    """
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def invalidate_matching_cache(*, reason: str = "") -> None:
    """Mark the shared snapshot stale after SKU data changed.

    Cheap and synchronous: the old snapshot keeps serving concurrent readers
    until the next request rebuilds it, so a write never blocks the dashboard.
    """
    global _version
    _version += 1
    suffix = f" ({reason})" if reason else ""
    logger.info("matching cache invalidated: version=%d%s", _version, suffix)


def reset_matching_cache() -> None:
    """Drop the snapshot entirely (used by tests)."""
    global _snapshot, _built_version
    _snapshot = None
    _built_version = -1


# ---------------------------------------------------------------------------
# Loading + normalizing
# ---------------------------------------------------------------------------

async def load_raw_skus(collection) -> List[Dict[str, Any]]:
    """Read the projected SKU documents (I/O only)."""
    return [doc async for doc in collection.find({}, PROJECTION)]


def normalize_documents(docs: Sequence[Dict[str, Any]]) -> List[Any]:
    """Build the colour vocabulary then normalize every document.

    Pure CPU work (no Mongo, no network), so callers can run it in a worker
    thread and keep the event loop free.
    """
    vocabulary = build_colour_vocabulary(docs)
    return [normalize_sku(doc, i, vocabulary) for i, doc in enumerate(docs)]


async def load_normalized_skus(collection) -> List[Any]:
    """Stream the collection and return normalized SKUs (pure data, no Mongo).

    Kept for callers that only need the normalized rows; the dashboard uses
    :func:`get_snapshot` so the normalization is shared and cached.
    """
    docs = await load_raw_skus(collection)
    return await asyncio.to_thread(normalize_documents, docs)


async def _build_snapshot(collection) -> MatchingSnapshot:
    """Load, normalize and cluster once, timing each phase separately."""
    started = time.perf_counter()

    t0 = time.perf_counter()
    docs = await load_raw_skus(collection)
    load_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    skus = await asyncio.to_thread(normalize_documents, docs)
    normalize_ms = (time.perf_counter() - t0) * 1000.0

    # One cluster build feeds both the metrics and the group list. ``compute_metrics``
    # accepts the prebuilt clusters so it never re-runs the union-find.
    t0 = time.perf_counter()
    clusters = await asyncio.to_thread(build_clusters, skus)
    cluster_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    metrics = await asyncio.to_thread(compute_metrics, skus, clusters)
    metrics_ms = (time.perf_counter() - t0) * 1000.0

    snapshot = MatchingSnapshot(
        metrics=metrics,
        clusters=clusters,
        total_skus=len(skus),
        built_at=time.time(),
        built_monotonic=time.monotonic(),
        load_ms=load_ms,
        normalize_ms=normalize_ms,
        cluster_ms=cluster_ms,
        metrics_ms=metrics_ms,
    )
    logger.info(
        "matching snapshot built in %.0fms "
        "(mongo_load=%.0fms normalize=%.0fms cluster=%.0fms metrics=%.0fms) "
        "skus=%d clusters=%d cross_shop=%d",
        (time.perf_counter() - started) * 1000.0,
        load_ms,
        normalize_ms,
        cluster_ms,
        metrics_ms,
        snapshot.total_skus,
        len(clusters),
        metrics.matched_groups,
    )
    return snapshot


async def get_snapshot(collection) -> tuple[MatchingSnapshot, bool]:
    """Return the current snapshot, building it if stale or missing.

    Concurrent callers share one build: the second waits on an async lock (the
    event loop stays free) and then reuses the first result. Returns
    ``(snapshot, cache_hit)``.
    """
    global _snapshot, _built_version

    lock = _get_lock()
    async with lock:
        now = time.monotonic()
        current = _snapshot
        if (
            current is not None
            and _built_version == _version
            and (now - current.built_monotonic) < CACHE_TTL_SECONDS
        ):
            return current, True

        # Capture the version before building: a write that lands mid-build
        # bumps it, so the result is still marked stale and rebuilt next time.
        version = _version
        built = await _build_snapshot(collection)
        _snapshot = built
        _built_version = version
        return built, False


# ---------------------------------------------------------------------------
# Public entry points (unchanged signatures)
# ---------------------------------------------------------------------------

async def get_metrics(collection) -> Metrics:
    snapshot, _ = await get_snapshot(collection)
    return snapshot.metrics


async def get_matched_groups(
    collection,
    limit: Optional[int] = 50,
    mixed_only: bool = False,
) -> List[Dict[str, Any]]:
    snapshot, _ = await get_snapshot(collection)
    return matched_group_samples(snapshot.clusters, limit=limit, mixed_only=mixed_only)
