"""Tests for the shared dashboard matching snapshot.

The two dashboard endpoints must not each scan and cluster the whole SKU
collection: they share one snapshot, and any SKU write invalidates it.
"""

import asyncio

import pytest

from app.matching import (
    get_matched_groups,
    get_metrics,
    invalidate_matching_cache,
    reset_matching_cache,
)
from app.matching.matcher import build_clusters, compute_metrics, normalize_sku


class _Cursor:
    def __init__(self, docs):
        self._iter = iter(docs)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    """Minimal Motor stand-in: counts how many full scans were performed."""

    def __init__(self, docs):
        self.docs = list(docs)
        self.scans = 0

    def find(self, query, projection):  # noqa: ARG002 - signature parity
        self.scans += 1
        return _Cursor(self.docs)


def _doc(shop, title, colour, size, in_stock=True):
    return {
        "shop": shop,
        "brand": "Carhartt WIP",
        "title": title,
        "colour": colour,
        "size": size,
        "in_stock": in_stock,
        "url": f"https://x.test/{shop}/{title}/{size}",
    }


DOCS = [
    _doc("a", "Chase Tee", "Black", "M", in_stock=True),
    _doc("b", "Chase Tee", "Black", "M", in_stock=False),
    _doc("a", "Solo Cap", "Red", "ONE_SIZE", in_stock=True),
]


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_matching_cache()
    yield
    reset_matching_cache()


@pytest.mark.asyncio
async def test_metrics_and_groups_share_one_scan():
    coll = _Collection(DOCS)

    metrics = await get_metrics(coll)
    await get_matched_groups(coll)

    assert coll.scans == 1
    assert metrics.total_skus == 3


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_build():
    coll = _Collection(DOCS)

    await asyncio.gather(get_metrics(coll), get_matched_groups(coll))

    assert coll.scans == 1


@pytest.mark.asyncio
async def test_invalidation_rebuilds_after_a_sku_change():
    coll = _Collection(DOCS)
    first = await get_metrics(coll)

    coll.docs = DOCS + [_doc("c", "Chase Tee", "Black", "M")]
    still_cached = await get_metrics(coll)
    assert still_cached.total_skus == first.total_skus
    assert coll.scans == 1

    invalidate_matching_cache(reason="test")
    fresh = await get_metrics(coll)
    assert fresh.total_skus == first.total_skus + 1
    assert coll.scans == 2


@pytest.mark.asyncio
async def test_groups_and_metrics_come_from_the_same_snapshot():
    coll = _Collection(DOCS)

    metrics = await get_metrics(coll)
    groups = await get_matched_groups(coll, limit=None, mixed_only=True)

    assert metrics.back_in_stock_opportunities == 1
    assert len(groups) == 1
    assert coll.scans == 1


def test_compute_metrics_accepts_prebuilt_clusters():
    skus = [normalize_sku(doc, i) for i, doc in enumerate(DOCS)]

    with_prebuilt = compute_metrics(skus, build_clusters(skus))
    computed_internally = compute_metrics(skus)

    assert with_prebuilt.to_dict() == computed_internally.to_dict()
