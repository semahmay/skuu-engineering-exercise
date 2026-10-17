"""Genuine back-in-stock detection.

The ``skus`` collection already stores each variant's ``in_stock`` flag, which
every scrape overwrites. A *genuine* restock is therefore an out-of-stock
``(url, size)`` that a refresh finds in stock again — a transition, not merely an
item that happens to be in stock.

Design constraints (from the exercise brief):

* A brand-new SKU has no history, so it is **not** a restock.
- A repeat refresh must not duplicate an event: ``restock_events`` has a unique
  ``(shop, url, size)`` index and writes are idempotent upserts.
- No history is fabricated: if the prior stock of a SKU is unknown it is
  skipped. Because the pipeline snapshots before scraping, that is only the case
  for SKUs never seen before (which are genuinely new, not restocked).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

StockKey = Tuple[str, str]  # (url, size)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def snapshot_stock(sku_collection: Any, shop_id: str) -> Dict[StockKey, bool]:
    """Return ``{(url, size): in_stock}`` for every SKU of ``shop_id``.

    Call this *before* scraping the shop so the values reflect the pre-refresh
    state.
    """
    prior: Dict[StockKey, bool] = {}
    cursor = sku_collection.find(
        {"shop": shop_id},
        {"url": 1, "size": 1, "in_stock": 1},
    )
    async for doc in cursor:
        url = doc.get("url")
        if url is None:
            continue
        prior[(url, doc.get("size"))] = bool(doc.get("in_stock"))
    return prior


async def detect_restocks(
    sku_collection: Any,
    shop_id: str,
    prior: Dict[StockKey, bool],
) -> List[Dict[str, Any]]:
    """Return events for SKUs that went out-of-stock -> in-stock since ``prior``."""
    events: List[Dict[str, Any]] = []
    cursor = sku_collection.find(
        {"shop": shop_id},
        {
            "url": 1,
            "size": 1,
            "in_stock": 1,
            "brand": 1,
            "title": 1,
            "colour": 1,
            "barcode": 1,
            "price": 1,
            "currency": 1,
        },
    )
    async for doc in cursor:
        url = doc.get("url")
        if url is None:
            continue
        key = (url, doc.get("size"))
        if key not in prior:
            continue  # new SKU: no history, not a restock
        if prior[key] or not bool(doc.get("in_stock")):
            continue  # was already in stock, or still out of stock
        events.append(
            {
                "shop": shop_id,
                "url": url,
                "size": doc.get("size"),
                "brand": doc.get("brand"),
                "title": doc.get("title"),
                "colour": doc.get("colour"),
                "barcode": doc.get("barcode"),
                "price": doc.get("price"),
                "currency": doc.get("currency"),
            }
        )
    return events


async def record_restocks(
    restock_collection: Any,
    events: List[Dict[str, Any]],
) -> int:
    """Persist restock events idempotently; return the number of *new* events.

    Uses the ``(shop, url, size)`` unique index. A re-detected restock updates
    ``last_detected_at`` but does not create a duplicate or count as new.
    """
    detected_at = _now_iso()
    new_events = 0
    for event in events:
        identity = {
            "shop": event["shop"],
            "url": event["url"],
            "size": event["size"],
        }
        result = await restock_collection.update_one(
            identity,
            {
                "$setOnInsert": {
                    **identity,
                    "brand": event.get("brand"),
                    "title": event.get("title"),
                    "colour": event.get("colour"),
                    "barcode": event.get("barcode"),
                    "price": event.get("price"),
                    "currency": event.get("currency"),
                    "first_detected_at": detected_at,
                },
                "$set": {"last_detected_at": detected_at},
            },
            upsert=True,
        )
        if getattr(result, "upserted_id", None) is not None:
            new_events += 1
    return new_events
