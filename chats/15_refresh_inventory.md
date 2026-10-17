# 15 — Refresh Inventory pipeline

Status: implemented and verified. Backend `233 passed` (non-DB), frontend
`npm test` 11 passed, `npm run lint` and `npm run build` clean.

## What was added

A single-call "Refresh Inventory" workflow that re-runs the whole catalogue and
reports live progress, built entirely on the existing scrapers, upsert and
matching engine — no new scraping or matching logic.

```
POST /api/v1/refresh          -> start (202), 409 if already running
GET  /api/v1/refresh/status   -> current/last job, {"status":"idle"} if never run
POST /api/v1/refresh/cancel   -> request cancellation (202), 409 if idle
```

Phases: `initializing → scraping → saving → matching → back_in_stock → metrics`,
then `completed` / `failed` / `cancelled`.

### Backend layout

| File | Role |
|------|------|
| `app/refresh/shops.py` | Registry of the 13 refreshable shops; lazy module import; stats normalisation |
| `app/refresh/job.py` | `RefreshJob` / `ShopResult` dataclasses + `to_dict()` for polling |
| `app/refresh/restock.py` | Prior-stock snapshot, transition detection, idempotent event writes |
| `app/refresh/pipeline.py` | Orchestration + cooperative cancellation |
| `app/refresh/manager.py` | Single-job lifecycle (`start` / `cancel` / `status`) |
| `app/api/refresh.py` | HTTP layer |
| `app/database/repository.py` | New `restock_events` collection + unique index |

## Key decisions

- **Persistence is not duplicated.** In this architecture each scraper already
  writes via `upsert_skus` as it crawls, so the `saving` phase *reports* the
  realised `skus_inserted` / `skus_updated` totals rather than performing a
  second write pass. New vs updated therefore comes straight from the existing
  upsert and cannot diverge from what is stored.
- **Progress is honest.** `progress = phase_index / phase_count`, advancing per
  completed shop during `scraping`. The scrapers do not expose per-variant
  progress, so the UI uses a real bar plus an indeterminate shimmer only within a
  shop — no fabricated percentage.
- **Genuine restocks only.** Before scraping a shop the pipeline snapshots
  `{(url, size): in_stock}`; afterwards only `False → True` transitions for SKUs
  that *already existed* become events. A newly discovered SKU has no history and
  is never a restock. Repeated refreshes are idempotent because `restock_events`
  has a unique `(shop, url, size)` index — a re-detected restock updates
  `last_detected_at` without creating a duplicate or counting as new.
- **Cooperative cancellation.** A cancel is observed immediately between
  shops/phases; while a scraper is mid-flight its task is cancelled via
  `asyncio.wait({scraper, cancel})`. Rows already upserted are kept — the run is
  reported `cancelled`/partial, never rolled back.
- **Failed ≠ refreshed.** A shop that raises, or returns a fatal catalogue
  sentinel (`sitemap` / `catalogue` / `products.json` with zero products), is
  listed in `failed_shops` and skipped for restock detection.
- **SSENSE excluded.** It is a documented no-op (Cloudflare 403); refreshing it
  would only add noise.

## Tests (`backend/tests/test_refresh.py`, 16 tests)

Fully isolated — an in-memory `FakeCollection` (async cursor + `update_one`
upsert) and fake scrapers; no MongoDB, no network, and the real pipeline is never
run. Coverage:

- pipeline order, totals accumulation, `progress == 1.0` on completion;
- new SKU is **not** a restock; OOS→in-stock **is**; duplicate refresh is idempotent;
- per-shop failure and fatal-sentinel failure are reported, not restocked;
- cancel before start and cancel during a run both end `cancelled`;
- single-job enforcement (`RefreshInProgress`) and cancel-without-job
  (`NoActiveRefresh`);
- HTTP: idle status, start/status/cancel round-trip, 409 on cancel with no job.

`frontend/scripts/refresh.test.ts` (6 tests) covers the pure helpers in
`src/lib/refresh.ts`: phase order, phase-state transitions, `mm:ss`/`h:mm:ss`
formatting and result labels.

## Frontend

`frontend/src/app/RefreshPanel.tsx` — a modal that starts a run, polls
`GET /refresh/status` every 900 ms, renders the phase stepper, progress bar,
elapsed time, live totals and failed shops, and switches the primary action to
**Cancel refresh** ("Cancelling…" while `status === "cancelling"`). On a terminal
state it reloads the dashboard metrics once. Styles are scoped under `.skuu-app`
in `globals.css` and reuse the existing tokens.

## Notable limitation

The `restock_events` log only begins at first refresh: an item that was already
out of stock before the first snapshot has history only from then on. This is the
smallest correct behaviour — we do not guess transitions we cannot observe.
