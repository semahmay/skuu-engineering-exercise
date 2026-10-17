# 18 — Refresh Inventory optimization (bounded concurrency + 13-shop registry)

Date: 2026-10-17
Status: implemented and verified. Backend `363 passed` (non-DB, excluding
`tests/test_database.py`).

> **Provenance.** This file is an **engineering summary**, not a verbatim
> transcript of the AI session. It was reconstructed from the verifiable
> repository changes, the in-repo benchmark artefacts and the test-suite output
> produced during the work. Where a number comes from a *simulated* (no-write)
> benchmark rather than a real end-to-end refresh, it is labelled as such.

## Objective

The "↻ Refresh inventory" button already re-scraped all shops sequentially. The
work in this phase made the pipeline use **bounded concurrency** instead of
strictly serial product processing, without changing what is scraped, matched or
stored — and then corrected the active shop registry from 14 to the **13
required** shops.

Nothing about the matching engine, matching thresholds, stock logic or the
refresh *semantics* changed. Only the scheduling of product-level work and the
registry contents changed.

## 1. Shared bounded concurrency (`backend/app/refresh/concurrency.py`)

Two independent ceilings combine so per-shop limits can never multiply into an
unbounded fan-out:

| Knob | Value | Where |
|------|-------|-------|
| Per-shop hard ceiling | `MAX_PRODUCT_CONCURRENCY = 5` | `concurrency.py` |
| Unconfigured default | `DEFAULT_PRODUCT_CONCURRENCY = 1` (sequential) | `concurrency.py` |
| Global refresh ceiling | `GLOBAL_PRODUCT_LIMIT = 6` | `concurrency.py`, env `SKUU_GLOBAL_PRODUCT_CONCURRENCY` |

- `SHOP_PRODUCT_CONCURRENCY` sets the per-shop limit:
  - **`carhartt_wip = 3`** — Playwright per-product navigation, measured safe and
    correct on a fixed sample;
  - **the HTTP / `products.json` retailers = 3** (asphaltgold, afew, beamhill,
    biffi, caliroots, footway, nugnes1920, sneakersnstuff, xtreme,
    salomon);
  - **`veja = 1`, `ssense = 1`** — Playwright per-product navigation, unmeasured
    / bot-gated, so they stay sequential.
- `SEQUENTIAL_ONLY_SHOPS = {"veja", "ssense"}` — these scrapers are always run
  with no concurrency kwargs (`run_shop` forwards `concurrency`/`gate` only to
  the others), so their single-page behaviour is untouched.
- `RefreshConcurrency` holds **one** `asyncio.Semaphore(GLOBAL_PRODUCT_LIMIT)`
  created once per refresh in `run_refresh` and handed to every shop. It also
  tracks live `active`/`peak` slots.
- `run_bounded(items, worker, limit=…, rc=…)` runs a **fixed worker pool** of at
  most `limit` coroutines (never one task per item — a 30 000-product catalogue
  cannot spawn 30 000 tasks). A worker raising is captured as
  `(False, exception)` for that item only; the rest still run.

**Why the global gate exists even though shops are still sequential.** The
pipeline deliberately scrapes shops **one at a time** (deterministic single
"current shop" progress, never several browsers at once); within a shop the
effective limit is that shop's limit. The shared semaphore is the guarantee that
the global ceiling still holds *if shops are ever overlapped* — it is what makes
the per-shop numbers safe to raise independently.

## 2. Pipeline wiring (`pipeline.py`, `job.py`, `shops.py`)

- `run_refresh` creates the one shared `RefreshConcurrency` and passes it into
  `_pipeline`; `_invoke` forwards `concurrency=rc.limit_for(shop)` and
  `gate=rc` **only** to the real `run_shop` (fake scrapers injected by the test
  suite still take just `spec`).
- `run_shop` forwards the kwargs only to non-`SEQUENTIAL_ONLY_SHOPS`.
- `RefreshJob.to_dict()` exposes a new `concurrency` telemetry block
  (`global_limit`, `per_shop` effective limits, `peak_active`) so a run reports
  the policy it actually used. `peak_active` is written after the last product
  task finishes.

## 3. Scraper changes

- **Carhartt WIP** (`scrapers/carhartt.py`): bounded product processing using the
  same `run_bounded` pool + shared gate, instead of one-at-a-time.
- **Shopify retailer core** (`scrapers/shopify_retailer.py`): `run_scraper_for(…,
  concurrency, gate)` bounds the per-product loop with
  `effective = min(clamp_limit(concurrency), gate.global_limit)`.
- **The HTTP shops' per-product work performs no extra request to the
  retailer** — the catalogue is already in memory from `/products.json` — so
  concurrency there only overlaps *independent local database upserts* on
  distinct `(shop, url, size)` keys. It cannot increase retailer load.

## 4. MongoDB SKU upsert optimization (`carhartt.upsert_skus`)

Identity is `(shop, url, size)` and the collection has a **unique** index on it.
The old path did a `find_one` pre-read and then an insert; the new path is a
**single conditional upsert**:

```python
result = await collection.update_one(key, {"$set": doc}, upsert=True)
if result.upserted_id is not None:  inserted += 1
else:                                updated += 1
```

- One round trip instead of a read plus a write.
- Atomic match-or-insert (the unique index makes it safe), so it is safe to call
  from several concurrent product tasks.
- A `DuplicateKeyError` from a racing insert is handled by falling back to a
  plain `update_one` (counted as *updated*), so concurrency never turns into a
  spurious failure.
- `inserted` / `updated` / `failed` still come straight from this one path, so
  the refresh "new vs updated" totals cannot diverge from what is stored.

## 5. Benchmarks — and their limitations

All performance numbers below are from **simulated, no-write** measurements that
isolate the database phase (the work a product task does after the catalogue is
in memory). They are **not** real end-to-end refresh timings.

| Measurement | Result |
|---|---|
| HTTP-shop DB phase, serial (conc 1) | 14.0 s |
| HTTP-shop DB phase, conc 3 | **4.63 s (3.03×)** |
| Same run with global gate = 2 | 6.98 s (2.0×) |
| SKU identities written, conc 1 vs conc 3 | **identical (900/900)** |

- The conc-3 speed-up is bounded by the number of independent database writes,
  not by any network parallelism — consistent with §3 (no extra retailer
  requests).
- Setting the shared gate below the per-shop limit correctly throttles back
  toward serial (2.0× at gate 2 vs 3.03× at gate 3+), which is the expected
  behaviour of the global ceiling.
- **Carhartt WIP is the honest exception.** In a prior *live* measurement over a
  fixed 15-URL sample, conc 3 was **not reliably faster** than serial (its cost
  is dominated by page navigation/browser work, not the local upsert), and a
  naive conc 10 was unsafe. That is exactly why `carhartt_wip = 3` is a *safety*
  cap validated for correctness, not a claimed speed-up.
- The global default of 6 is a conservative ceiling that is never actually
  reached in the sequential-shop production flow (the current shop's limit is
  the binding constraint); it exists for the overlap case described in §1.

## 6. Registry correction: 14 → 13 required shops

The exercise requires **exactly 13 websites**: 3 brands + 10 retailers. The
registry had picked up a 14th entry, `overkillshop` (Overkill, Berlin), which had
never been run (0 stored rows) and was not part of the required set.

- `backend/app/refresh/shops.py` — the `overkillshop` `ShopSpec` was removed from
  `SHOPS`. `job.total_shops = len(shops)` derives from this list, so the refresh
  now reports 13.
- The Overkill **scraper implementation** (`scrapers/overkillshop.py`), its
  scraper tests (still exercised in `test_shopify_retailer.py`), and any
  historical records were **left untouched** — only the active registry entry
  was removed. The (now unused) `"overkillshop": 3` entry in
  `SHOP_PRODUCT_CONCURRENCY` was kept so the scraper still works if run manually.
- Resulting registry: **Carhartt WIP, VEJA, Salomon** (brands) +
  **Afew Store, Asphaltgold, Beamhill, Biffi, Caliroots, Footway, Nugnes 1920,
  Sneakersnstuff, SSENSE, Xtreme** (retailers). SSENSE stays in (it is one of
  the ten required retailers).

## 7. Tests

```
cd backend
python -m pytest tests/ -q --ignore=tests/test_database.py    # 363 passed
```

- `backend/tests/test_concurrency.py` (new, +16) covers the policy: clamping,
  per-shop limits, the global gate lowering an effective limit, `peak_active`
  tracking, `run_bounded` bounding a pool (never one task per item), and one
  worker raising not cancelling the others.
- Concurrency coverage was added to `test_carhartt_scraper.py`,
  `test_shopify_retailer.py`, `test_salomon_scraper.py`,
  `test_asphaltgold_scraper.py` and `test_refresh.py`.
- These are **isolated unit tests** — fake scrapers, in-memory collections, no
  MongoDB and no network. They pin the *policy* and the *upsert idempotence*, not
  live wall-clock behaviour.
- `tests/test_database.py` writes to the live `skuu` database and is deliberately
  excluded.

## 8. Files

| File | Role |
|------|------|
| `backend/app/refresh/concurrency.py` | New: policy, `RefreshConcurrency`, `run_bounded` |
| `backend/app/refresh/pipeline.py` | Creates the shared gate; forwards it to shops |
| `backend/app/refresh/shops.py` | Registry (now the **13** required shops); forwards concurrency |
| `backend/app/refresh/job.py` | `concurrency` telemetry in `to_dict()` |
| `backend/app/scrapers/carhartt.py` | Bounded product pool; atomic upsert |
| `backend/app/scrapers/shopify_retailer.py` | `run_scraper_for(..., concurrency, gate)` |
| `backend/tests/test_concurrency.py` | New policy / `run_bounded` tests (+16) |
| `backend/tests/test_*.py` | Concurrency assertions added to the scraper/refresh suites |
| `README.md` | Refresh architecture, concurrency, registry note |

## 9. Known limitations (unchanged by this work)

- Concurrency does not remove the fundamental cost of scraping: Carhartt/VEJA/
  SSENSE still navigate one product page per product (VEJA and SSENSE stay
  sequential), and the HTTP shops' saving cost is dominated by MongoDB writes.
- The global ceiling is a safety net for shop overlap that the current sequential
  scheduler does not exercise.
- Cancellation is unchanged: it is cooperative, and SKUs written before a cancel
  are kept (a cancelled refresh is a partial refresh, never a rollback).
