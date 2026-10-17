"""Refresh Inventory pipeline tests.

Fully isolated: an in-memory fake collection stands in for Motor, fake scrapers
stand in for the real ones, and no network, MongoDB or real refresh is touched.

Covered: pipeline order/totals, new-vs-restock transitions, duplicate-event
idempotency, partial failures, fatal scraper sentinels, cancellation (before and
during a run), single-job enforcement, and the HTTP endpoints.
"""

import asyncio
import importlib

import pytest

from app.refresh.job import RefreshJob, ShopResult
from app.refresh.manager import NoActiveRefresh, RefreshInProgress, RefreshManager
from app.refresh.pipeline import failure_reason, run_refresh, scraper_seconds
from app.refresh.shops import ShopSpec, scraper_failed, stats_to_totals


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeUpdateResult:
    def __init__(self, upserted_id=None):
        self.upserted_id = upserted_id


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    """Minimal in-memory stand-in for a Motor collection."""

    def __init__(self, docs=None):
        self.docs = [dict(d) for d in (docs or [])]

    @staticmethod
    def _match(doc, flt):
        return all(doc.get(k) == v for k, v in flt.items())

    def find(self, query=None, projection=None):
        query = query or {}
        out = []
        for doc in self.docs:
            if not self._match(doc, query):
                continue
            if projection:
                out.append({k: doc.get(k) for k in projection if k != "_id"})
            else:
                out.append(dict(doc))
        return FakeCursor(out)

    async def update_one(self, flt, update, upsert=False):
        for doc in self.docs:
            if self._match(doc, flt):
                for key, value in update.get("$set", {}).items():
                    doc[key] = value
                return FakeUpdateResult(None)
        if upsert:
            new = dict(flt)
            new.update(update.get("$setOnInsert", {}))
            new.update(update.get("$set", {}))
            self.docs.append(new)
            return FakeUpdateResult(f"generated-{len(self.docs)}")
        return FakeUpdateResult(None)


class FakeMetrics:
    def __init__(self, matched_skus=0, matched_groups=0, back_in_stock_skus=0):
        self.matched_skus = matched_skus
        self.matched_groups = matched_groups
        self.back_in_stock_skus = back_in_stock_skus


def metrics_fn(metrics=None):
    async def _fn(_collection):
        return metrics or FakeMetrics()

    return _fn


def sku(shop, url, size, in_stock, **extra):
    doc = {"shop": shop, "url": url, "size": size, "in_stock": in_stock}
    doc.update(extra)
    return doc


TWO_SHOPS = [
    ShopSpec("shop_a", "Shop A", "retailer", "fake.a"),
    ShopSpec("shop_b", "Shop B", "retailer", "fake.b"),
]


def make_scraper(collection, plans):
    """Fake scraper applying per-shop plans and returning stats.

    A plan is a dict that may contain:
    - ``raise``: an exception instance to raise;
    - ``set_stock``: list of ``(url, size, in_stock)`` mutations;
    - ``add_skus``: list of brand-new SKU docs to append (simulating discovery);
    - ``stats``: the stats dict to return.
    """

    async def scraper(spec):
        plan = plans.get(spec.shop_id, {})
        if "raise" in plan:
            raise plan["raise"]
        for doc in plan.get("add_skus", []):
            collection.docs.append(dict(doc))
        for url, size, value in plan.get("set_stock", []):
            for doc in collection.docs:
                if doc["shop"] == spec.shop_id and doc["url"] == url and doc["size"] == size:
                    doc["in_stock"] = value
        return plan.get("stats", {})

    return scraper


async def run_pipeline(collection, restocks, plans, *, shops=None, metrics=None, cancel_event=None, concurrency=None):
    job = RefreshJob(id="test-job")
    await run_refresh(
        job,
        shops=shops or TWO_SHOPS,
        cancel_event=cancel_event,
        scraper=make_scraper(collection, plans),
        sku_collection=collection,
        restock_collection=restocks,
        metrics_fn=metrics_fn(metrics),
        concurrency=concurrency,
    )
    return job


# --------------------------------------------------------------------------
# shops helpers
# --------------------------------------------------------------------------


def test_stats_to_totals_normalises_missing_keys():
    assert stats_to_totals({}) == (0, 0, 0, 0)
    assert stats_to_totals(
        {"products_discovered": 3, "skus_inserted": 1, "skus_updated": 2, "skus_failed": 0}
    ) == (3, 1, 2, 0)


def test_scraper_failed_only_for_fatal_sentinels():
    assert scraper_failed({"failed_urls": [("sitemap", "timeout")]})
    assert scraper_failed({"failed_urls": [("products.json", "boom")]})
    assert not scraper_failed({"products_discovered": 5, "failed_urls": [("sitemap", "x")]})
    assert not scraper_failed({"failed_urls": [("https://x/p", "http_404")]})


# --------------------------------------------------------------------------
# failure_reason (diagnosability helper)
# --------------------------------------------------------------------------


def test_failure_reason_extracts_first_entry_reason():
    assert failure_reason({"failed_urls": [("sitemap", "timeout")]}) == "timeout"
    assert (
        failure_reason(
            {"failed_urls": [("sitemap", "RuntimeError: Failed to fetch sitemap: 403")]}
        )
        == "RuntimeError: Failed to fetch sitemap: 403"
    )
    # Only the first entry is preserved, and whitespace is collapsed.
    assert (
        failure_reason({"failed_urls": [("catalogue", "  a\n b  "), ("sitemap", "x")]})
        == "a b"
    )
    # A bare-string entry is used verbatim.
    assert failure_reason({"failed_urls": ["sitemap"]}) == "sitemap"


def test_failure_reason_is_safe_for_missing_or_malformed():
    assert failure_reason({}) is None
    assert failure_reason({"failed_urls": []}) is None
    assert failure_reason({"failed_urls": None}) is None
    assert failure_reason({"failed_urls": [("sitemap", "")]}) is None
    assert failure_reason({"failed_urls": [("sitemap", "   ")]}) is None
    assert failure_reason({"failed_urls": [None]}) is None
    assert failure_reason({"failed_urls": [("sitemap",)]}) is None
    assert failure_reason({"failed_urls": [object()]}) is None
    assert failure_reason("not-a-dict") is None
    assert failure_reason(None) is None



# --------------------------------------------------------------------------
# pipeline behaviour
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_completes_and_accumulates_totals():
    collection = FakeCollection([sku("shop_a", "u1", "M", True), sku("shop_b", "u2", "L", True)])
    restocks = FakeCollection()
    plans = {
        "shop_a": {"stats": {"products_discovered": 2, "skus_inserted": 1, "skus_updated": 1}},
        "shop_b": {"stats": {"products_discovered": 1, "skus_updated": 1}},
    }
    job = await run_pipeline(collection, restocks, plans, metrics=FakeMetrics(10, 5, 4))

    assert job.status == "completed"
    assert job.phase == "metrics"
    assert job.completed_shops == 2
    assert job.failed_shops == []
    assert job.totals["skus_inserted"] == 1
    assert job.totals["skus_updated"] == 2
    assert job.totals["matched_skus"] == 10
    assert job.totals["match_groups"] == 5
    assert job.totals["back_in_stock"] == 4
    assert job.progress == 1.0


@pytest.mark.asyncio
async def test_pipeline_records_shared_concurrency_telemetry():
    from app.refresh.concurrency import RefreshConcurrency

    collection = FakeCollection()
    restocks = FakeCollection()
    rc = RefreshConcurrency(global_limit=4)

    job = await run_pipeline(collection, restocks, {}, concurrency=rc)

    assert job.status == "completed"
    assert job.concurrency["global_limit"] == 4
    assert set(job.concurrency["per_shop"]) == {"shop_a", "shop_b"}
    # The injected fake scrapers never touch the gate, so nothing was in flight.
    assert job.concurrency["peak_active"] == 0
    assert job.to_dict()["concurrency"] == job.concurrency


@pytest.mark.asyncio
async def test_new_sku_is_not_a_restock():
    collection = FakeCollection()  # empty before the scrape
    restocks = FakeCollection()
    new_doc = sku("shop_a", "u1", "M", True, brand="Carhartt")
    plans = {"shop_a": {"add_skus": [sku("shop_a", "u1", "M", True, brand="Carhartt")]}}

    job = await run_pipeline(collection, restocks, plans)
    assert job.totals["restocked_events"] == 0
    assert restocks.docs == []


@pytest.mark.asyncio
async def test_restock_detected_on_oos_to_in_stock():
    collection = FakeCollection([sku("shop_a", "u1", "M", False, brand="Carhartt")])
    restocks = FakeCollection()
    plans = {"shop_a": {"set_stock": [("u1", "M", True)]}}

    job = await run_pipeline(collection, restocks, plans)
    assert job.totals["restocked_events"] == 1
    assert len(restocks.docs) == 1
    event = restocks.docs[0]
    assert (event["shop"], event["url"], event["size"]) == ("shop_a", "u1", "M")
    assert "first_detected_at" in event and "last_detected_at" in event


@pytest.mark.asyncio
async def test_duplicate_restock_not_recorded_twice():
    collection = FakeCollection([sku("shop_a", "u1", "M", False)])
    restocks = FakeCollection()
    plans = {"shop_a": {"set_stock": [("u1", "M", True)]}}

    first = await run_pipeline(collection, restocks, plans)
    assert first.totals["restocked_events"] == 1
    first_detected = restocks.docs[0]["first_detected_at"]

    second = await run_pipeline(collection, restocks, plans)
    assert second.totals["restocked_events"] == 0
    assert len(restocks.docs) == 1
    assert restocks.docs[0]["first_detected_at"] == first_detected


@pytest.mark.asyncio
async def test_restock_equivalence_through_real_upsert_path(monkeypatch):
    """The production ``upsert_skus`` still yields exactly one restock event.

    This exercises the real persistence path (not a fake scraper) against an
    in-memory collection, proving the single-upsert change preserves
    snapshot/detect/record semantics and deduplication.
    """
    from app.models.sku import SKU
    from app.refresh.restock import detect_restocks, record_restocks, snapshot_stock
    from app.scrapers.carhartt import upsert_skus

    collection = FakeCollection([sku("shop_a", "u1", "M", False, brand="Carhartt")])
    restocks = FakeCollection()

    async def _get_collection():
        return collection

    monkeypatch.setattr("app.scrapers.carhartt.get_sku_collection", _get_collection)

    prior = await snapshot_stock(collection, "shop_a")
    assert prior == {("u1", "M"): False}

    now_in_stock = SKU(
        shop="shop_a",
        brand="Carhartt",
        title="Jacket",
        colour="Black",
        size="M",
        price=199.0,
        currency="EUR",
        barcode=None,
        in_stock=True,
        url="u1",
    )
    inserted, updated, failed = await upsert_skus([now_in_stock])
    assert (inserted, updated, failed) == (0, 1, 0)

    events = await detect_restocks(collection, "shop_a", prior)
    assert len(events) == 1
    assert await record_restocks(restocks, events) == 1

    # Repeating detection/recording must not duplicate the event.
    events_again = await detect_restocks(collection, "shop_a", prior)
    assert await record_restocks(restocks, events_again) == 0
    assert len(restocks.docs) == 1


@pytest.mark.asyncio
async def test_failed_shop_is_reported_and_not_restocked():
    collection = FakeCollection([sku("shop_a", "u1", "M", False)])
    restocks = FakeCollection()
    plans = {"shop_a": {"raise": RuntimeError("boom")}}

    job = await run_pipeline(collection, restocks, plans, shops=[TWO_SHOPS[0]])
    assert job.status == "completed"
    assert job.failed_shops == ["shop_a"]
    assert restocks.docs == []
    assert job.shops[0].ok is False


@pytest.mark.asyncio
async def test_fatal_scraper_sentinel_marks_shop_failed():
    collection = FakeCollection()
    restocks = FakeCollection()
    plans = {"shop_a": {"stats": {"failed_urls": [("sitemap", "timeout")]}}}

    job = await run_pipeline(collection, restocks, plans, shops=[TWO_SHOPS[0]])
    assert job.failed_shops == ["shop_a"]
    assert job.shops[0].ok is False


@pytest.mark.asyncio
async def test_fatal_sentinel_records_failure_reason():
    collection = FakeCollection()
    restocks = FakeCollection()
    plans = {
        "shop_a": {
            "stats": {
                "products_discovered": 0,
                "failed_urls": [("sitemap", "RuntimeError: Failed to fetch sitemap: 403")],
            }
        }
    }

    job = await run_pipeline(collection, restocks, plans, shops=[TWO_SHOPS[0]])
    assert job.failed_shops == ["shop_a"]
    result = job.shops[0]
    assert result.ok is False
    assert result.error == "RuntimeError: Failed to fetch sitemap: 403"


@pytest.mark.asyncio
async def test_missing_or_empty_failed_urls_is_not_a_failure():
    plans_missing = {"shop_a": {"stats": {"products_discovered": 0}}}
    job = await run_pipeline(
        FakeCollection(), FakeCollection(), plans_missing, shops=[TWO_SHOPS[0]]
    )
    assert job.failed_shops == []
    assert job.shops[0].ok is True
    assert job.shops[0].error is None

    plans_empty = {"shop_a": {"stats": {"products_discovered": 0, "failed_urls": []}}}
    job2 = await run_pipeline(
        FakeCollection(), FakeCollection(), plans_empty, shops=[TWO_SHOPS[0]]
    )
    assert job2.failed_shops == []
    assert job2.shops[0].ok is True
    assert job2.shops[0].error is None


@pytest.mark.asyncio
async def test_exception_records_error_and_counts_completed():
    collection = FakeCollection()
    restocks = FakeCollection()
    plans = {
        "shop_a": {"stats": {"products_discovered": 1}},
        "shop_b": {"raise": RuntimeError("boom")},
    }
    job = await run_pipeline(collection, restocks, plans)

    assert job.status == "completed"
    assert job.completed_shops == 2
    assert job.failed_shops == ["shop_b"]
    assert [s.shop for s in job.shops] == ["shop_a", "shop_b"]
    assert job.shops[0].error is None
    assert job.shops[1].ok is False
    assert job.shops[1].error == "boom"


@pytest.mark.asyncio
async def test_middle_exception_keeps_completed_shops_correct():
    collection = FakeCollection()
    restocks = FakeCollection()
    shops = [
        ShopSpec("s1", "S1", "retailer", "fake.1"),
        ShopSpec("s2", "S2", "retailer", "fake.2"),
        ShopSpec("s3", "S3", "retailer", "fake.3"),
    ]
    plans = {"s2": {"raise": RuntimeError("mid")}}

    job = await run_pipeline(collection, restocks, plans, shops=shops)
    assert job.completed_shops == 3
    assert job.failed_shops == ["s2"]
    assert [s.shop for s in job.shops] == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_early_exception_completed_shops_not_inflated():
    collection = FakeCollection()
    restocks = FakeCollection()
    plans = {"shop_a": {"raise": RuntimeError("early")}}

    job = await run_pipeline(collection, restocks, plans)
    assert job.completed_shops == 2
    assert job.failed_shops == ["shop_a"]
    assert job.shops[0].ok is False
    assert job.shops[1].ok is True



@pytest.mark.asyncio
async def test_empty_exception_message_falls_back_to_class_name():
    collection = FakeCollection()
    restocks = FakeCollection()
    plans = {"shop_a": {"raise": NotImplementedError()}}

    job = await run_pipeline(collection, restocks, plans, shops=[TWO_SHOPS[0]])
    assert job.failed_shops == ["shop_a"]
    assert job.shops[0].ok is False
    assert job.shops[0].error == "NotImplementedError"


@pytest.mark.asyncio
async def test_progress_advances_with_completed_shops():
    collection = FakeCollection()
    restocks = FakeCollection()
    job = RefreshJob(id="p")
    setattr(job, "_progress_samples", [])

    phases = []

    async def scraper(spec):
        phases.append(job.phase)
        return {}

    await run_refresh(
        job,
        shops=TWO_SHOPS,
        scraper=scraper,
        sku_collection=collection,
        restock_collection=restocks,
        metrics_fn=metrics_fn(),
    )
    assert job.status == "completed"
    assert phases == ["scraping", "scraping"]
    assert job.progress == 1.0


@pytest.mark.asyncio
async def test_cancel_before_scraping_finishes_cancelled():
    collection = FakeCollection()
    restocks = FakeCollection()
    cancel = asyncio.Event()
    cancel.set()

    job = await run_pipeline(collection, restocks, {}, cancel_event=cancel)
    assert job.status == "cancelled"
    assert job.cancelled is True
    assert job.completed_shops == 0


# --------------------------------------------------------------------------
# timing telemetry (Phase 0 instrumentation)
# --------------------------------------------------------------------------


def test_scraper_seconds_helper_is_safe():
    assert scraper_seconds({"elapsed_seconds": 4}) == 4.0
    assert scraper_seconds({"elapsed_seconds": 2.5}) == 2.5
    assert scraper_seconds({"elapsed_seconds": "2.5"}) == 2.5
    assert scraper_seconds({"elapsed_seconds": -3}) == 0.0
    assert scraper_seconds({}) is None
    assert scraper_seconds({"elapsed_seconds": None}) is None
    assert scraper_seconds({"elapsed_seconds": "abc"}) is None
    assert scraper_seconds({"elapsed_seconds": object()}) is None
    assert scraper_seconds(None) is None


def test_shop_result_to_dict_exposes_timings():
    result = ShopResult(shop="s", ok=True, scraper_seconds=1.5, total_seconds=2.0)
    payload = result.to_dict()
    assert payload["scraper_seconds"] == 1.5
    assert payload["total_seconds"] == 2.0
    # Defaults are None, never a fabricated zero.
    assert ShopResult(shop="s").to_dict()["total_seconds"] is None
    assert ShopResult(shop="s").to_dict()["scraper_seconds"] is None


@pytest.mark.asyncio
async def test_pipeline_records_scraper_and_total_seconds():
    collection = FakeCollection()
    restocks = FakeCollection()
    plans = {
        "shop_a": {"stats": {"products_discovered": 1, "elapsed_seconds": 12.5}},
        "shop_b": {"stats": {"products_discovered": 1, "elapsed_seconds": 3}},
    }

    job = await run_pipeline(collection, restocks, plans)
    a, b = job.shops
    assert a.scraper_seconds == 12.5
    assert b.scraper_seconds == 3.0
    # Total wall-clock is measured independently of the scraper's own number.
    assert a.total_seconds is not None and a.total_seconds >= 0
    assert b.total_seconds is not None and b.total_seconds >= 0
    assert a.to_dict()["total_seconds"] == a.total_seconds


@pytest.mark.asyncio
async def test_failed_shop_has_total_seconds_but_no_scraper_seconds():
    collection = FakeCollection()
    restocks = FakeCollection()
    plans = {"shop_a": {"raise": RuntimeError("boom")}}

    job = await run_pipeline(collection, restocks, plans, shops=[TWO_SHOPS[0]])
    result = job.shops[0]
    assert result.ok is False
    assert result.scraper_seconds is None
    assert result.total_seconds is not None and result.total_seconds >= 0


@pytest.mark.asyncio
async def test_phase_durations_recorded_on_completion():
    collection = FakeCollection()
    restocks = FakeCollection()
    job = await run_pipeline(collection, restocks, {"shop_a": {}, "shop_b": {}})

    assert job.status == "completed"
    # Every pipeline phase is timed, including scraping and matching.
    for phase in ("initializing", "scraping", "saving", "matching", "back_in_stock", "metrics"):
        assert phase in job.phase_durations
        assert job.phase_durations[phase] >= 0
    assert job.to_dict()["phase_durations"] == job.phase_durations


@pytest.mark.asyncio
async def test_elapsed_seconds_frozen_after_finish():
    job = RefreshJob(id="t")
    job.set_phase("scraping", "Scraping")
    job.finish("completed")
    first = job.elapsed_seconds
    assert job.to_dict()["elapsed_seconds"] == first
    await asyncio.sleep(0.02)
    assert job.elapsed_seconds == first
    assert job.to_dict()["elapsed_seconds"] == first


def test_current_shop_elapsed_tracks_a_live_shop():
    job = RefreshJob(id="t")
    assert job.current_shop_elapsed_seconds is None
    job.begin_shop("shop_a")
    assert job.current_shop == "shop_a"
    assert job.current_shop_elapsed_seconds is not None
    job.end_shop()
    assert job.current_shop is None
    assert job.current_shop_elapsed_seconds is None


@pytest.mark.asyncio
async def test_cancel_mid_shop_clears_current_shop_and_times_partial_phase():
    collection = FakeCollection()
    restocks = FakeCollection()
    cancel = asyncio.Event()
    entered = asyncio.Event()

    async def slow_scraper(spec):
        entered.set()
        await asyncio.sleep(30)
        return {}

    job = RefreshJob(id="cancel-mid")
    task = asyncio.ensure_future(
        run_refresh(
            job,
            shops=[TWO_SHOPS[0]],
            cancel_event=cancel,
            scraper=slow_scraper,
            sku_collection=collection,
            restock_collection=restocks,
            metrics_fn=metrics_fn(),
        )
    )
    await entered.wait()
    assert job.current_shop == "shop_a"
    assert job.current_shop_elapsed_seconds is not None

    cancel.set()
    await task

    assert job.status == "cancelled"
    assert job.cancelled is True
    # No permanently-running indicator or timer survives cancellation.
    assert job.current_shop is None
    assert job.current_shop_elapsed_seconds is None
    assert "scraping" in job.phase_durations
    assert job.phase_durations["scraping"] >= 0


# --------------------------------------------------------------------------
# manager
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manager_prevents_duplicate_jobs():
    mgr = RefreshManager()
    collection = FakeCollection()

    async def slow_scraper(spec):
        await asyncio.sleep(0.2)
        return {}

    await mgr.start(
        shops=TWO_SHOPS,
        scraper=slow_scraper,
        sku_collection=collection,
        restock_collection=FakeCollection(),
        metrics_fn=metrics_fn(),
    )
    try:
        with pytest.raises(RefreshInProgress):
            await mgr.start(shops=TWO_SHOPS, scraper=slow_scraper)
    finally:
        await mgr.cancel()
        await mgr.wait()


@pytest.mark.asyncio
async def test_manager_cancel_transitions_to_cancelled():
    mgr = RefreshManager()
    collection = FakeCollection()

    async def slow_scraper(spec):
        await asyncio.sleep(5)
        return {}

    job = await mgr.start(
        shops=TWO_SHOPS,
        scraper=slow_scraper,
        sku_collection=collection,
        restock_collection=FakeCollection(),
        metrics_fn=metrics_fn(),
    )
    await asyncio.sleep(0.05)
    await mgr.cancel()
    assert job.status == "cancelling"
    await mgr.wait()
    assert job.status == "cancelled"


@pytest.mark.asyncio
async def test_manager_cancel_without_active_job_raises():
    mgr = RefreshManager()
    with pytest.raises(NoActiveRefresh):
        await mgr.cancel()


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


async def _client():
    from httpx import AsyncClient, ASGITransport
    from app.main import app

    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_refresh_status_endpoint_when_idle(monkeypatch):
    mgr = RefreshManager()
    monkeypatch.setattr("app.api.refresh.manager", mgr)

    async with await _client() as client:
        response = await client.get("/api/v1/refresh/status")
    assert response.status_code == 200
    assert response.json() == {"status": "idle"}


@pytest.mark.asyncio
async def test_refresh_start_and_cancel_endpoints(monkeypatch):
    mgr = RefreshManager()

    async def fake_run_refresh(job, **kwargs):
        await asyncio.sleep(5)
        job.finish("completed")
        return job

    monkeypatch.setattr("app.api.refresh.manager", mgr)
    manager_module = importlib.import_module("app.refresh.manager")
    monkeypatch.setattr(manager_module, "run_refresh", fake_run_refresh)

    async with await _client() as client:
        started = await client.post("/api/v1/refresh")
        assert started.status_code == 202
        assert started.json()["status"] == "running"

        status = await client.get("/api/v1/refresh/status")
        assert status.json()["status"] == "running"

        cancelled = await client.post("/api/v1/refresh/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json()["status"] in ("cancelling", "cancelled")

    await mgr.cancel()
    await mgr.wait()


@pytest.mark.asyncio
async def test_refresh_cancel_without_active_job_is_409(monkeypatch):
    mgr = RefreshManager()
    monkeypatch.setattr("app.api.refresh.manager", mgr)

    async with await _client() as client:
        response = await client.post("/api/v1/refresh/cancel")
    assert response.status_code == 409
