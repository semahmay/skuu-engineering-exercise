"""Dashboard metrics API — cross-shop matching results.

Read-only endpoints computed on demand from the ``skus`` collection.

Both endpoints read one shared, cached snapshot (see ``app.matching.service``),
so they no longer each scan and cluster the whole collection. Each request logs
its total elapsed time plus whether it reused the snapshot, and each snapshot
build logs the MongoDB load, normalization, clustering and metrics phases
separately so a slow phase is identifiable from the Render log stream.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query

from app.database import get_sku_collection
from app.matching import get_snapshot
from app.matching.matcher import matched_group_samples

logger = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])


def _iso_z(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@router.get("/metrics")
async def dashboard_metrics() -> dict:
    """The four dashboard metrics plus per-shop / per-brand breakdowns."""
    started = time.perf_counter()
    collection = await get_sku_collection()
    snapshot, cache_hit = await get_snapshot(collection)

    payload = snapshot.metrics.to_dict()
    # Fixed to the moment the snapshot was computed rather than request time:
    # the dashboard's "updated at" must describe the data, not the fetch.
    payload["generated_at"] = _iso_z(snapshot.built_at)
    payload["cache"] = snapshot.cache_info(hit=cache_hit)

    logger.info(
        "GET /api/v1/metrics 200 in %.0fms (cache_hit=%s age=%.1fs "
        "skus=%d clusters=%d)",
        (time.perf_counter() - started) * 1000.0,
        cache_hit,
        time.monotonic() - snapshot.built_monotonic,
        snapshot.total_skus,
        len(snapshot.clusters),
    )
    return payload


@router.get("/matching/groups")
async def matched_groups(
    limit: Optional[int] = Query(default=None, ge=1, le=10000),
    mixed_only: bool = Query(default=True),
):
    """Cross-shop matched product groups.

    By default only *back-in-stock opportunities* (mixed stock across shops) are
    returned and, with no ``limit``, the whole set is returned. That keeps the
    dashboard's detail list consistent with the headline opportunity count
    instead of truncating an arbitrary sample of groups.
    """
    started = time.perf_counter()
    collection = await get_sku_collection()
    snapshot, cache_hit = await get_snapshot(collection)
    groups = matched_group_samples(
        snapshot.clusters, limit=limit, mixed_only=mixed_only
    )

    logger.info(
        "GET /api/v1/matching/groups 200 in %.0fms (cache_hit=%s age=%.1fs "
        "groups=%d limit=%s mixed_only=%s)",
        (time.perf_counter() - started) * 1000.0,
        cache_hit,
        time.monotonic() - snapshot.built_monotonic,
        len(groups),
        limit if limit is not None else "all",
        mixed_only,
    )
    return {"count": len(groups), "groups": groups}
