"""Dashboard metrics API — cross-shop matching results.

Read-only endpoints computed on demand from the ``skus`` collection.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from app.database import get_sku_collection
from app.matching import ALGORITHM_VERSION, get_matched_groups, get_metrics

router = APIRouter(tags=["metrics"])


@router.get("/metrics")
async def dashboard_metrics() -> dict:
    """The four dashboard metrics plus per-shop / per-brand breakdowns."""
    collection = await get_sku_collection()
    metrics = await get_metrics(collection)
    payload = metrics.to_dict()
    payload["generated_at"] = __import__("datetime").datetime.utcnow().isoformat() + "Z"
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
    collection = await get_sku_collection()
    groups = await get_matched_groups(collection, limit=limit, mixed_only=mixed_only)
    return {"count": len(groups), "groups": groups}
