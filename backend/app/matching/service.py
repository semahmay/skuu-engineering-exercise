"""Database-backed entry points for the matching engine.

Loads SKU documents from MongoDB, normalizes them, runs the matcher and returns
the dashboard metrics. Read-only: nothing here mutates the collection.
"""

from typing import Any, Dict, List, Optional

from app.matching.matcher import (
    Metrics,
    build_clusters,
    build_colour_vocabulary,
    compute_metrics,
    matched_group_samples,
    normalize_sku,
)

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


async def load_normalized_skus(collection) -> List[Any]:
    """Stream the collection and return normalized SKUs (pure data, no Mongo).

    A colour vocabulary is built from every published ``colour`` field in the
    dataset first, then used to conservatively derive colours for shops that
    only expose the colourway in the URL handle. Nothing is written back.
    """
    docs = [doc async for doc in collection.find({}, PROJECTION)]
    vocabulary = build_colour_vocabulary(docs)
    return [normalize_sku(doc, i, vocabulary) for i, doc in enumerate(docs)]


async def get_metrics(collection) -> Metrics:
    skus = await load_normalized_skus(collection)
    return compute_metrics(skus)


async def get_matched_groups(
    collection,
    limit: Optional[int] = 50,
    mixed_only: bool = False,
) -> List[Dict[str, Any]]:
    skus = await load_normalized_skus(collection)
    clusters = build_clusters(skus)
    return matched_group_samples(clusters, limit=limit, mixed_only=mixed_only)
