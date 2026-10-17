"""Shared bounded-concurrency policy tests.

Everything here is in-memory: no network, no MongoDB, no real scraper. Workers
are fakes that record how many run at once and which asyncio task they run in, so
the guarantees (per-shop limit, shared global limit, bounded task creation,
per-item isolation, slot release on cancellation) are asserted directly.
"""

import asyncio

import pytest

from app.refresh.concurrency import (
    DEFAULT_PRODUCT_CONCURRENCY,
    MAX_PRODUCT_CONCURRENCY,
    SEQUENTIAL_ONLY_SHOPS,
    RefreshConcurrency,
    clamp_limit,
    run_bounded,
    shop_limit,
)


# ---------------------------------------------------------------------------
# Policy: clamping and per-shop limits
# ---------------------------------------------------------------------------

def test_clamp_limit_bounds_to_one_and_hard_cap():
    assert clamp_limit(0) == 1
    assert clamp_limit(-5) == 1
    assert clamp_limit(1) == 1
    assert clamp_limit(3) == 3
    assert clamp_limit(MAX_PRODUCT_CONCURRENCY) == MAX_PRODUCT_CONCURRENCY
    assert clamp_limit(999) == MAX_PRODUCT_CONCURRENCY
    # tolerant of strings and junk
    assert clamp_limit("3") == 3
    assert clamp_limit("nonsense") == DEFAULT_PRODUCT_CONCURRENCY
    assert clamp_limit(None) == DEFAULT_PRODUCT_CONCURRENCY


def test_shop_limit_uses_conservative_defaults():
    # Playwright per-product shop measured safe at 3.
    assert shop_limit("carhartt_wip") == 3
    # HTTP catalogue shops overlap only local DB writes.
    assert shop_limit("afew") == 3
    assert shop_limit("asphaltgold") == 3
    assert shop_limit("salomon") == 3
    # Unmeasured / bot-gated Playwright shops stay sequential.
    assert shop_limit("veja") == 1
    assert shop_limit("ssense") == 1
    assert "veja" in SEQUENTIAL_ONLY_SHOPS
    assert "ssense" in SEQUENTIAL_ONLY_SHOPS
    # Unknown shop -> sequential.
    assert shop_limit("some-new-shop") == DEFAULT_PRODUCT_CONCURRENCY


def test_limit_for_never_exceeds_global():
    rc = RefreshConcurrency(global_limit=2)
    assert rc.global_limit == 2
    assert rc.limit_for("carhartt_wip") == 2  # min(3, 2)
    assert rc.limit_for("afew") == 2
    # sequential-only shops ignore the global allowance above 1
    assert rc.limit_for("veja") == 1

    small = RefreshConcurrency(global_limit=1)
    assert small.limit_for("carhartt_wip") == 1


def test_refresh_concurrency_snapshot():
    rc = RefreshConcurrency(global_limit=4)
    assert rc.snapshot() == {"global_limit": 4, "active": 0, "peak": 0}


# ---------------------------------------------------------------------------
# run_bounded: per-shop limit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_bounded_never_exceeds_per_shop_limit():
    live = {"n": 0, "peak": 0}

    async def worker(i):
        live["n"] += 1
        live["peak"] = max(live["peak"], live["n"])
        await asyncio.sleep(0.01)
        live["n"] -= 1
        return i

    outcomes = await run_bounded(list(range(20)), worker, limit=3)

    assert live["peak"] == 3
    assert all(ok for ok, _ in outcomes)
    assert [value for _, value in outcomes] == list(range(20))


@pytest.mark.asyncio
async def test_run_bounded_limit_one_is_sequential_and_ordered():
    order = []

    async def worker(i):
        order.append(i)
        await asyncio.sleep(0)
        return i

    outcomes = await run_bounded(list(range(6)), worker, limit=1)

    assert order == [0, 1, 2, 3, 4, 5]
    assert [value for _, value in outcomes] == [0, 1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_run_bounded_creates_a_fixed_pool_not_one_task_per_item():
    task_ids = set()

    async def worker(i):
        task_ids.add(id(asyncio.current_task()))
        await asyncio.sleep(0)
        return i

    await run_bounded(list(range(50)), worker, limit=3)

    # 50 items, but only a pool of ``limit`` worker tasks is ever created.
    assert len(task_ids) == 3


@pytest.mark.asyncio
async def test_run_bounded_empty_input():
    async def worker(i):  # pragma: no cover - never called
        raise AssertionError("should not run")

    assert await run_bounded([], worker, limit=3) == []


# ---------------------------------------------------------------------------
# run_bounded: shared global gate across shops
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_gate_is_shared_across_concurrent_shops():
    rc = RefreshConcurrency(global_limit=4)

    async def worker(i):
        await asyncio.sleep(0.02)
        return i

    async def shop(prefix):
        return await run_bounded(list(range(10)), worker, limit=3, rc=rc)

    results = await asyncio.gather(shop("a"), shop("b"), shop("c"))

    # Four in flight saturate the global ceiling but never exceed it, even though
    # three shops each asked for three.
    assert rc.peak == 4
    assert rc.active == 0
    assert all(len(r) == 10 for r in results)


@pytest.mark.asyncio
async def test_global_gate_one_serialises_everything():
    rc = RefreshConcurrency(global_limit=1)
    live = {"n": 0, "peak": 0}

    async def worker(i):
        live["n"] += 1
        live["peak"] = max(live["peak"], live["n"])
        await asyncio.sleep(0.005)
        live["n"] -= 1
        return i

    await asyncio.gather(
        run_bounded(list(range(5)), worker, limit=3, rc=rc),
        run_bounded(list(range(5)), worker, limit=3, rc=rc),
    )

    assert live["peak"] == 1
    assert rc.peak == 1


# ---------------------------------------------------------------------------
# run_bounded: per-item isolation and cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_bounded_isolates_failures_and_preserves_input_order():
    async def worker(i):
        if i == 3:
            raise ValueError("boom")
        return i

    outcomes = await run_bounded(list(range(6)), worker, limit=4)

    assert outcomes[0] == (True, 0)
    ok, err = outcomes[3]
    assert ok is False and isinstance(err, ValueError)
    # Every other item still ran and results line up with their input index.
    expected = [0, 1, 2, 4, 5]
    got = [value for i, (flag, value) in enumerate(outcomes) if i != 3 and flag]
    assert got == expected


@pytest.mark.asyncio
async def test_run_bounded_releases_global_slots_on_cancellation():
    rc = RefreshConcurrency(global_limit=3)
    started = asyncio.Event()

    async def worker(i):
        started.set()
        await asyncio.sleep(30)

    task = asyncio.ensure_future(run_bounded(list(range(10)), worker, limit=3, rc=rc))
    await started.wait()
    await asyncio.sleep(0.01)
    assert rc.active > 0

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The ``slot`` context managers ran their finally blocks despite the cancel.
    assert rc.active == 0


# ---------------------------------------------------------------------------
# run_shop wiring: which scrapers receive the policy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_shop_forwards_policy_to_capable_scraper(monkeypatch):
    from app.refresh import shops

    recorded = {}

    async def fake_scraper(max_products=None, **kwargs):
        recorded["max_products"] = max_products
        recorded.update(kwargs)
        return {}

    monkeypatch.setattr(shops, "load_scraper", lambda spec: fake_scraper)
    spec = shops.ShopSpec("afew", "Afew Store", "retailer", "app.scrapers.afew")
    gate = RefreshConcurrency(global_limit=5)

    await shops.run_shop(spec, concurrency=3, gate=gate)

    assert recorded["max_products"] is None
    assert recorded["concurrency"] == 3
    assert recorded["gate"] is gate


@pytest.mark.asyncio
async def test_run_shop_uses_policy_default_when_concurrency_omitted(monkeypatch):
    from app.refresh import shops

    recorded = {}

    async def fake_scraper(max_products=None, **kwargs):
        recorded.update(kwargs)
        return {}

    monkeypatch.setattr(shops, "load_scraper", lambda spec: fake_scraper)
    spec = shops.ShopSpec("carhartt_wip", "Carhartt WIP", "brand", "app.scrapers.carhartt")

    await shops.run_shop(spec)

    assert recorded["concurrency"] == shop_limit("carhartt_wip")
    assert recorded["gate"] is None


@pytest.mark.asyncio
async def test_run_shop_keeps_sequential_only_shops_sequential(monkeypatch):
    from app.refresh import shops

    recorded = {}

    async def fake_scraper(max_products=None, **kwargs):
        recorded.update(kwargs)
        return {}

    monkeypatch.setattr(shops, "load_scraper", lambda spec: fake_scraper)
    spec = shops.ShopSpec("veja", "VEJA", "brand", "app.scrapers.veja")

    await shops.run_shop(spec, concurrency=5, gate=RefreshConcurrency(global_limit=5))

    assert "concurrency" not in recorded
    assert "gate" not in recorded


@pytest.mark.asyncio
async def test_run_shop_preserves_extra_scraper_kwargs(monkeypatch):
    from app.refresh import shops

    recorded = {}

    async def fake_scraper(max_products=None, **kwargs):
        recorded.update(kwargs)
        return {}

    monkeypatch.setattr(shops, "load_scraper", lambda spec: fake_scraper)
    spec = shops.ShopSpec(
        "salomon", "Salomon", "brand", "app.scrapers.salomon", {"store_key": "salomon_fr"}
    )

    await shops.run_shop(spec, concurrency=2)

    assert recorded["store_key"] == "salomon_fr"
    assert recorded["concurrency"] == 2
