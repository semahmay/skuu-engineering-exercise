"""The "Refresh Inventory" pipeline.

Runs the existing scrapers (which persist via the existing ``upsert_skus``
path), then reuses the existing matching engine and records genuine restock
events. Nothing here re-implements scraping or matching; it only sequences the
existing pieces and reports real progress.

Cancellation is cooperative: between shops and between phases it stops
immediately; while a scraper is running the in-flight task is cancelled (the
scraper may have already persisted some SKUs — that is expected and is why a
cancelled run is reported as partial, not rolled back).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.refresh.concurrency import RefreshConcurrency
from app.refresh.job import RefreshJob, ShopResult
from app.refresh.restock import detect_restocks, record_restocks, snapshot_stock
from app.refresh.shops import SHOPS, run_shop, scraper_failed, stats_to_totals
from app.matching.service import invalidate_matching_cache


class RefreshCancelled(Exception):
    """Raised internally when a cancel is observed mid-pipeline."""


def _raise_if_cancelled(job: RefreshJob, cancel_event) -> None:
    if job.cancelled or (cancel_event is not None and cancel_event.is_set()):
        raise RefreshCancelled()


def failure_reason(stats: Any) -> Optional[str]:
    """Best-effort failure reason from a scraper's ``failed_urls``.

    Scrapers record a fatal catalogue failure as a ``("sitemap", reason)`` /
    ``("catalogue", reason)`` / ``("products.json", reason)`` entry. This returns
    the first entry's reason (whitespace-collapsed, length-capped) or ``None``
    when no usable reason is present. It never raises: a missing, empty or
    malformed ``failed_urls`` degrades to ``None`` so error reporting cannot
    itself break a refresh, and no reason is invented when none is available.
    """
    if not isinstance(stats, dict):
        return None
    entries = stats.get("failed_urls")
    if not entries:
        return None
    try:
        first = entries[0]
    except (IndexError, KeyError, TypeError):
        return None
    reason: Any = None
    if isinstance(first, str):
        reason = first
    elif isinstance(first, (list, tuple)) and len(first) >= 2:
        reason = first[1]
    if reason is None:
        return None
    text = " ".join(str(reason).split())
    return text[:200] or None


def scraper_seconds(stats: Any) -> Optional[float]:
    """The scraper's self-reported runtime from ``stats["elapsed_seconds"]``.

    Read-only convenience for telemetry: returns a non-negative float (rounded
    to 0.01s) or ``None`` when the key is absent, not a number, or negative.
    It never raises, so a malformed timing cannot break a refresh.
    """
    if not isinstance(stats, dict):
        return None
    value = stats.get("elapsed_seconds")
    if value is None:
        return None
    try:
        return round(max(0.0, float(value)), 2)
    except (TypeError, ValueError):
        return None


async def _run_scraper_cancellable(job: RefreshJob, cancel_event, coro):
    """Await ``coro`` but abort promptly if ``cancel_event`` is set."""
    task = asyncio.ensure_future(coro)
    if cancel_event is None:
        return await task
    cancel_task = asyncio.ensure_future(cancel_event.wait())
    done, _ = await asyncio.wait(
        {task, cancel_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if cancel_task in done and task not in done:
        task.cancel()
        try:
            await task
        except (Exception, asyncio.CancelledError):  # noqa: BLE001
            pass
        cancel_task.cancel()
        raise RefreshCancelled()
    cancel_task.cancel()
    return task.result()


async def run_refresh(
    job: RefreshJob,
    *,
    shops=None,
    cancel_event=None,
    scraper=None,
    sku_collection=None,
    restock_collection=None,
    metrics_fn=None,
    concurrency: Optional[RefreshConcurrency] = None,
) -> RefreshJob:
    """Execute the full pipeline, mutating ``job`` as it progresses.

    All dependencies are injectable so the test suite can run the pipeline with
    fake scrapers and an in-memory collection, never touching MongoDB or the
    network. Production callers omit them and the real implementations are used.

    ``concurrency`` is the one shared :class:`RefreshConcurrency` gate for the
    whole run; when omitted a default one is created. It is handed to every shop
    so per-shop limits can never multiply into an unbounded fan-out.
    """
    shops = SHOPS if shops is None else shops
    scraper = run_shop if scraper is None else scraper
    metrics_fn = metrics_fn or _load_metrics

    if sku_collection is None:
        from app.database import get_sku_collection

        sku_collection = await get_sku_collection()
    if restock_collection is None:
        from app.database import get_restock_collection

        restock_collection = await get_restock_collection()

    try:
        await _pipeline(
            job,
            shops=shops,
            cancel_event=cancel_event,
            scraper=scraper,
            sku_collection=sku_collection,
            restock_collection=restock_collection,
            metrics_fn=metrics_fn,
            concurrency=concurrency,
        )
    except RefreshCancelled:
        job.cancelled = True
        job.end_shop()
        job.finish("cancelled", "Refresh cancelled")
        # Scrapers may have persisted rows before the cancellation.
        invalidate_matching_cache(reason="refresh cancelled")
    except Exception as exc:  # noqa: BLE001
        job.end_shop()
        job.finish("failed", f"{type(exc).__name__}: {exc}")
        # The run may have written SKUs before it failed; never leave the
        # dashboard serving a pre-refresh snapshot.
        invalidate_matching_cache(reason="refresh failed")
    return job


async def _load_metrics(collection):
    from app.matching.service import get_metrics

    return await get_metrics(collection)


async def _pipeline(
    job: RefreshJob,
    *,
    shops,
    cancel_event,
    scraper,
    sku_collection,
    restock_collection,
    metrics_fn,
    concurrency: Optional[RefreshConcurrency] = None,
) -> None:
    job.total_shops = len(shops)
    job.set_phase("initializing", f"Preparing {len(shops)} shops")
    _raise_if_cancelled(job, cancel_event)

    # One shared gate for the whole refresh. Shops are still scheduled
    # sequentially, but every product task draws from this single ceiling, so the
    # per-shop limits below can never multiply into an unbounded fan-out.
    rc = concurrency if concurrency is not None else RefreshConcurrency()
    job.concurrency = {
        "global_limit": rc.global_limit,
        "per_shop": {spec.shop_id: rc.limit_for(spec.shop_id) for spec in shops},
        "peak_active": 0,
    }

    def _invoke(spec):
        # Fake scrapers injected by tests take only ``spec``; only the real
        # ``run_shop`` understands the shared concurrency policy.
        if scraper is run_shop:
            return scraper(spec, concurrency=rc.limit_for(spec.shop_id), gate=rc)
        return scraper(spec)

    # 2. Scrape every shop, snapshotting its prior stock first so restocks are
    #    detected against the pre-refresh state.
    job.set_phase("scraping", "Scraping brands and retailers")
    for index, spec in enumerate(shops, 1):
        _raise_if_cancelled(job, cancel_event)
        job.begin_shop(spec.shop_id)
        job.message = f"Scraping {spec.label} ({index}/{len(shops)})"
        # Wall-clock for the whole per-shop unit (snapshot + scrape + restock),
        # distinct from the scraper's own self-reported runtime.
        shop_started = time.monotonic()

        prior = await snapshot_stock(sku_collection, spec.shop_id)
        try:
            stats = await _run_scraper_cancellable(job, cancel_event, _invoke(spec))
        except RefreshCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc).strip() or type(exc).__name__
            job.shops.append(
                ShopResult(
                    shop=spec.shop_id,
                    ok=False,
                    error=message,
                    total_seconds=round(time.monotonic() - shop_started, 2),
                )
            )
            job.failed_shops.append(spec.shop_id)
            job.completed_shops = index
            job.end_shop()
            # A failing scraper may still have persisted some rows before raising.
            invalidate_matching_cache(reason=f"refresh:{spec.shop_id}")
            continue

        discovered, inserted, updated, failed = stats_to_totals(stats)
        ok = not scraper_failed(stats)
        job.shops.append(
            ShopResult(
                shop=spec.shop_id,
                ok=ok,
                products_discovered=discovered,
                skus_inserted=inserted,
                skus_updated=updated,
                skus_failed=failed,
                error=None if ok else failure_reason(stats),
                scraper_seconds=scraper_seconds(stats),
            )
        )
        job.totals["products_discovered"] += discovered
        job.totals["skus_inserted"] += inserted
        job.totals["skus_updated"] += updated
        job.totals["skus_failed"] += failed

        if ok:
            events = await detect_restocks(sku_collection, spec.shop_id, prior)
            job.totals["restocked_events"] += await record_restocks(restock_collection, events)
        else:
            job.failed_shops.append(spec.shop_id)

        # Close the per-shop total only after restock detection so it captures
        # the full wall-clock cost of the shop, not just the scrape.
        job.shops[-1].total_seconds = round(time.monotonic() - shop_started, 2)

        job.completed_shops = index
        job.end_shop()
        # This shop's upserts changed the SKU data; rebuild the dashboard
        # snapshot on the next request instead of serving pre-refresh numbers.
        invalidate_matching_cache(reason=f"refresh:{spec.shop_id}")

    _raise_if_cancelled(job, cancel_event)

    # Record the realised peak now that no product task is in flight.
    job.concurrency["peak_active"] = rc.peak

    # 3. Persistence already happened inside each scraper's upsert; report it.
    job.set_phase("saving", "Recording new and updated SKU totals")

    # 4. Recompute cross-shop matches with the existing engine.
    job.set_phase("matching", "Matching products across shops")
    metrics = await metrics_fn(sku_collection)
    job.totals["matched_skus"] = int(getattr(metrics, "matched_skus", 0) or 0)
    job.totals["match_groups"] = int(getattr(metrics, "matched_groups", 0) or 0)

    # 5. Back-in-stock opportunities (existing metric) + restock events recorded.
    job.set_phase("back_in_stock", "Checking back-in-stock opportunities")
    job.totals["back_in_stock"] = int(getattr(metrics, "back_in_stock_skus", 0) or 0)

    # 6. Final metrics step.
    job.set_phase("metrics", "Updating dashboard metrics")
    job.end_shop()

    summary = (
        f"Refreshed {len(shops) - len(job.failed_shops)}/{len(shops)} shops"
        f" (+{job.totals['skus_inserted']} new, "
        f"{job.totals['skus_updated']} updated)"
    )
    job.finish("completed", summary)
