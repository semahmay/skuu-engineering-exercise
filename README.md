# Product Inventory Matching System

An inventory-intelligence system for fashion retail. It collects product
inventory from online stores, extracts product- and size-level information,
detects per-size stock availability, normalizes SKUs, matches the same product
across retailers, and identifies back-in-stock opportunities. All inventory is
stored in MongoDB.

> **SKU definition:** one SKU = one product + one size + one shop.
> A six-size product therefore produces six SKU records.

## Current status

- **62,647 SKUs** across **13 shops** (three brands: Carhartt WIP, Veja, Salomon).
- **7,393 matched SKUs** (11.8010% coverage), **3,580 cross-shop groups**.
- **362 back-in-stock opportunities** (447 sold-out SKUs recoverable elsewhere).
- Backend tests: **363 passing** (excluding `backend/tests/test_database.py`,
  which writes to the **live** `skuu` database — see [Testing](#testing)).
- Frontend `npm test`, `npm run lint` and `npm run build` succeed.
- Matching engine `algorithm_version = 1.4.1`.
- A **1.2 MB demo snapshot** of the full catalogue ships with the repository
  (`demo/skuu_demo.archive.gz`) so the dashboard can be opened **without
  scraping** — see [Demo database](#demo-database-snapshot--restore).

## Features

- **Inventory collection** from e-commerce storefronts (public pages, headless Chromium) and Shopify `/products.json`.
- **Size-level extraction** from the product DOM, not just aggregated product data.
- **Per-size stock detection**, including partially sold-out products.
- **JSON-LD fallback** for products whose size selector is not rendered.
- **SKU normalization** into a single document shape per size.
- **Cross-shop matching** across 13 shops (same product + size + colourway).
- **Back-in-stock detection**: a product sold out in one shop but in stock in another.
- **Refresh Inventory pipeline**: one call re-scrapes every shop, records new/updated
  SKUs, recomputes matches and logs genuine out-of-stock → in-stock transitions,
  with live progress and cancellation (`POST /api/v1/refresh`).
- **Dashboard** (`/api/v1/metrics`, `/api/v1/matching/groups`) and a Next.js UI.
- **MongoDB storage** with idempotent upserts and duplicate protection on `(shop, url, size)`.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Scraping | Python, Playwright (headless Chromium), httpx (Shopify JSON) |
| API | FastAPI, Pydantic |
| Database | MongoDB (Motor async driver) |
| Frontend | Next.js, React, TypeScript, Tailwind CSS |
| Testing | pytest, pytest-asyncio |

## Shops

| Shop | Platform | Currency | SKUs | Notes |
|------|----------|----------|-----:|-------|
| `carhartt_wip` | Carhartt WIP DTC | EUR | 4,403 | Playwright + JSON-LD |
| `veja` | Veja DTC | EUR | 14,681 | Playwright |
| `salomon` | Salomon DTC | AUD | 9,923 | sitemap/JSON feed |
| `asphaltgold` | Shopify retailer | EUR | 2,587 | `/products.json` |
| `sneakersnstuff` | Shopify retailer | EUR | 884 | `/products.json` |
| `afew` | Shopify retailer | EUR | 4,280 | `/products.json` |
| `footway` | Shopify retailer | SEK | 4,092 | `/products.json` |
| `xtreme` | Shopify retailer | EUR | 2,879 | `/products.json` |
| `biffi` | Shopify retailer | EUR | 465 | `/products.json` |
| `beamhill` | Shopify retailer | EUR | 1,411 | `/products.json` |
| `caliroots` | Shopify retailer | SEK | 3,983 | `/products.json` |
| `nugnes1920` | Shopify retailer | EUR | 714 | `/products.json` |
| `ssense` | proprietary (Playwright) | EUR | 2,929 | product **sitemap** + JSON-LD |

**SSENSE is scraped via a headless browser.** SSENSE is not a Shopify store, so
the `/products.json` feed used for the other retailers does not exist (HTTP 404),
and its catalogue and product pages answer **HTTP 403** to a plain HTTP client
(`server: cloudflare`, `cf-mitigated: challenge`). A normal headless Chromium
session clears the challenge by executing the page's own JavaScript — no CAPTCHA
solving, proxy, fingerprint spoofing or stolen-cookie tricks — so
`backend/app/scrapers/ssense.py` drives Playwright, discovers products from the
declared product **sitemap** (the `?page=` query needed for category crawling is
disallowed by `robots.txt`, so the sitemap is the complete compliant source) and
extracts each product's fields from its JSON-LD plus the size `<select>` and the
`Supplier color` line. Only the two brands this project tracks are kept
(Carhartt WIP, Veja), in **EUR** (`/en-de`). `probe()` reports reachability and
`run_scraper()` upserts SKUs like every other shop.

**`overkillshop`** (Overkill, Berlin — Shopify, EUR, `carhartt-wip` / `salomon` /
`veja` collections) is an extra Shopify retailer scraper that is **not** one of the
13 websites required by the exercise (3 brands + 10 retailers) and is **not** part
of the active refresh registry in `app/refresh/shops.py`. The module is retained
and can still be run manually via `python -m app.scrapers.overkillshop`.

## Dashboard metrics

Computed read-only on demand from the `skus` collection (`GET /api/v1/metrics`):

| Metric | Definition | Live value |
|--------|------------|-----------:|
| `total_skus` | every SKU record | 62,647 |
| `matched_skus` | SKUs in a cluster spanning ≥2 shops | 7,393 |
| `coverage_pct` | `matched_skus / total_skus × 100` | 11.8010 |
| `back_in_stock_opportunities` | cross-shop clusters with **mixed** stock (in stock in ≥1 shop, sold out in ≥1 other) | 362 |

Extras returned alongside: `back_in_stock_skus` (447 sold-out members of those
clusters), `matched_groups` (3,580), `shops`, and per-shop / per-brand
breakdowns.

The three matching figures are different units — do not conflate them:

- **`matched_skus` (7,393)** counts *SKU rows*: every product+size+shop row that
  belongs to a group spanning ≥2 shops. One matched product in 3 shops at 5 sizes
  contributes 15 matched SKUs.
- **`matched_groups` (3,580)** counts *groups*: one group = one product+size that
  appears in ≥2 shops (regardless of how many shops or rows it has).
- **`back_in_stock_opportunities` (362)** counts a *subset* of those groups: the
  ones whose stock is **mixed** across shops (in stock somewhere, sold out
  somewhere else). `back_in_stock_skus` (447) is the sold-out row count inside
  those 362 groups. Coverage is `matched_skus / total_skus`.

## Refresh Inventory pipeline

A single background job re-runs the whole catalogue pipeline. It is started from
the dashboard ("↻ Refresh inventory") and exposes three endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/refresh` | Start a run (202); `409` if one is already running |
| `GET`  | `/api/v1/refresh/status` | Current/last job state; `{"status":"idle"}` if never run |
| `POST` | `/api/v1/refresh/cancel` | Request cancellation (202); `409` if nothing is running |

Phases (each reported in `phase` / `phase_label`): **initializing → scraping →
saving → matching → back_in_stock → metrics**, ending in `completed`, `failed` or
`cancelled`. `progress` is `completed_phases / total_phases` (advancing per shop
during scraping) — real work only, never fabricated.

- **One job at a time.** A second `POST /refresh` is rejected with `409`; the job
  state is `running` → `cancelling` → `cancelled`.
- **Cancellation is cooperative.** It is observed between shops/phases
  immediately, and cancels the in-flight scraper task otherwise. SKUs written
  before the cancel are kept (the run is a *partial* refresh, not a rollback).
- **Persistence reuses the existing upsert** (`(shop, url, size)`), so new vs
  updated is always separated and a shop that fails to scrape is reported as
  failed, not "refreshed".
- **Genuine restocks only.** Before scraping a shop the pipeline snapshots the
  prior `{(url, size): in_stock}`; afterwards only real `out-of-stock → in-stock`
  transitions become `restock_events` (see below). A brand-new SKU is never a
  restock, and a repeated refresh never duplicates an event (unique
  `(shop, url, size)` index).

Implementation: `backend/app/refresh/` (`pipeline.py`, `manager.py`, `restock.py`,
`shops.py`, `job.py`, `concurrency.py`) and `backend/app/api/refresh.py`.

### Bounded concurrency and upserts

Product-level work inside each shop runs through a fixed, bounded worker pool
instead of one task per product, under two ceilings: a **per-shop** limit
(`SHOP_PRODUCT_CONCURRENCY`, hard-capped at 5) and a **single global** limit
(`RefreshConcurrency`, default 6, env `SKUU_GLOBAL_PRODUCT_CONCURRENCY`) shared
by the whole run. Shops are still scraped **one at a time**; the global gate is
the safety net that holds the ceiling if shops are ever overlapped. Carhartt WIP
and the HTTP `/products.json` retailers run at 3; VEJA and SSENSE (Playwright,
per-product navigation) stay sequential. For the HTTP shops, concurrency only
overlaps **independent local database upserts** — the catalogue is already in
memory, so it adds **no** extra request to the retailer.

Each SKU is written with a single conditional upsert on the unique
`(shop, url, size)` identity (`update_one(key, {"$set": …}, upsert=True)`), so
the write is atomic, needs no `find_one` pre-read, and is safe under concurrency
(a racing insert falls back to a plain update).

Full background — the per-shop policy, the benchmark numbers (and why they are
simulated, not live) and the 14→13 registry correction — is in
[`chats/18_refresh_inventory_optimization.md`](chats/18_refresh_inventory_optimization.md).

## Matching engine

Implementation in `backend/app/matching/` (`normalization.py`, `matcher.py`,
`service.py`). Two SKUs are the *same product* when, across **different shops**,
they agree on:

1. **Brand** — required and non-empty (canonical: `Carhartt` → `Carhartt WIP`).
2. **Size** — required and non-empty after normalization. SSENSE's explicit
   `WAIST US …` and `IT …` prefixes are stripped to the bare number (so its
   `WAIST US 28 x 32` meets another shop's `28/32`). No UK↔EU conversion in
   general; the single exception is Salomon footwear, whose DTC store publishes
   UK sizes while EU retailers publish EU sizes — those are mapped to the EU
   system so the brand's own store can be compared with its retailers.
3. **Identity** — an exact `normalize(title + colour)` **composite** match (so a
   shop that folds colour into the title matches one that keeps it in the colour
   field), **or** equal normalized titles **when both sides carry a compatible
   colour**, **or** equal **base keys** (the title with its own colour words
   removed and a trailing generic noun trimmed, e.g. `Sneakers`) **when at least
   one side begins with its colour** (the SSENSE `Black OG Active Jacket` shape),
   colours are compatible, **and** the prefixed side's *leading colour run* is
   corroborated by the other side's colour. An empty colour is **not** a wildcard.
   Corroboration is what stops a shared *accent* word from bridging different
   colourways — SSENSE `Green Prescott Coat` (`Oxide green/Black`) must not match
   a `Black / Black` coat just because both strings contain `black`.
4. **Colour** — when both sides have a colour, one word set must be a **subset**
   of the other (`Black` vs `Black Rinsed`); genuinely different colourways are
   rejected. When a shop publishes no colour, a colourway may be **conservatively
   derived from the product URL handle** (see below).
5. **Style code** — when both sides carry a manufacturer style code they must be
   equal.

Additional conservative rules:

- Matching is only ever across different shops.
- **Ambiguous groups are dropped** — if one shop lists multiple
  indistinguishable products under the same key, the group is excluded rather
  than guessed at.
- Empty/missing values are never treated as match identifiers.
- **Handle colour is a last resort and equality-only.** It is used only when the
  scraped `colour` field is empty, is accepted only if every trailing handle word
  after the title is a known colour word (built from the colours shops actually
  publish), and the scraped field always wins. Because a handle can abbreviate a
  colour (`...-landon-pant-blue252` → `blue`), a derived colour must *equal* the
  other side's colour — never a subset — so a bare `blue` cannot bridge
  `Blue Rinsed` and `Blue Marble Used Wash`. The derived value is stored
  separately and never overwrites scraped data.

See [`chats/08_matching_engine.md`](chats/08_matching_engine.md),
[`chats/09_retailer_expansion.md`](chats/09_retailer_expansion.md),
[`chats/10_matching_validation.md`](chats/10_matching_validation.md),
[`chats/11_colour_extraction.md`](chats/11_colour_extraction.md) and
[`chats/17_ssense_matching.md`](chats/17_ssense_matching.md).

## Repository Layout

```
.
├── backend/                 # FastAPI service, scrapers, matching, tests
│   ├── app/
│   │   ├── main.py          # FastAPI app + lifespan + CORS
│   │   ├── config.py        # Settings (environment variables)
│   │   ├── api/             # HTTP routes (health, metrics, refresh)
│   │   ├── database/        # Motor client, SKU + restock repositories, indexes
│   │   ├── matching/        # Cross-shop matching + metrics
│   │   ├── models/          # Pydantic SKU models
│   │   ├── refresh/         # Refresh Inventory pipeline + job manager
│   │   └── scrapers/        # Per-shop scrapers (DTC + Shopify retailers)
│   ├── scripts/             # demo_db.py (snapshot), export_skus.py / import_skus.py
│   ├── tests/               # pytest suite
│   └── requirements.txt
├── demo/                    # skuu_demo.archive.gz — populated demo snapshot
├── frontend/                # Next.js dashboard
└── chats/                   # Development logs
```

## Setup

### Quick start (populated demo, no scraping)

To run the full stack against the packaged snapshot **without scraping any
website** (this is the flow a new machine should follow):

```bash
# 1. clone
git clone <repo-url> skuu && cd skuu

# 2. backend dependencies
cd backend
python -m pip install -r requirements.txt

# 3. point the app at a dedicated demo database
copy .env.example .env            # Windows;  cp on macOS/Linux
# then edit .env and set MONGODB_DB=skuu_demo

# 4. start MongoDB (see below), then import the packaged snapshot
python scripts/demo_db.py import --db skuu_demo --drop

# 5. run the API
python -m uvicorn app.main:app --port 8000   # do NOT add --reload on Windows

# 6. in a second terminal, run the dashboard
cd ../frontend
npm install
npm run dev                                 # http://localhost:3000
```

The dashboard then shows the restored inventory immediately. Each step is
explained in detail below; Playwright/Chromium (`python -m playwright install
chromium`) is **only** needed if a refresh is later started manually.

### Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.11+ | backend + scrapers |
| Node.js | 20+ (18 min) | Next.js frontend |
| MongoDB | 6+ (tested on 8.2) | local server on `localhost:27017` |
| Git | any | |

No MongoDB **Database Tools** are required — demo data is exported/imported with
the bundled Python scripts (see [Demo database](#demo-database-snapshot--restore)).

### MongoDB

Install MongoDB Community Server and make sure `mongod` is running
(`mongodb://localhost:27017`). On Windows it usually runs as a service:

```powershell
# check the service
Get-Service MongoDB
# start it if stopped (admin shell)
Start-Service MongoDB
```

The demo snapshot's collections (`skus`, `restock_events`) and **all their
indexes** are created by the restore step (`scripts/demo_db.py import`, see
[Demo database](#demo-database-snapshot--restore)). A database built by scraping
from scratch creates the collections on first write; the index definitions live
in `backend/app/database/repository.py` (`create_indexes`) and are applied by
the restore script and the test suite.

### Backend

```bash
cd backend
python -m pip install -r requirements.txt
python -m playwright install chromium      # only needed to run the scrapers

# optional: copy the env template (defaults already point at localhost)
copy .env.example .env                     # Windows;  cp on macOS/Linux
```

```env
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=skuu
CORS_ORIGINS=["http://localhost:3000","http://127.0.0.1:3000"]
```

Run the API:

```bash
cd backend
python -m uvicorn app.main:app --port 8000
# health:   http://localhost:8000/api/v1/health
# metrics:  http://localhost:8000/api/v1/metrics
# refresh:  POST http://localhost:8000/api/v1/refresh
```

> **Windows:** do **not** add `--reload` (or `--workers`). On Windows uvicorn
> switches those to the `SelectorEventLoop`, which cannot spawn the subprocess
> Playwright needs, so the Playwright-based scrapers (Carhartt WIP, VEJA) fail
> with `NotImplementedError` and are reported as failed/skipped. Running without
> `--reload` keeps the default `ProactorEventLoop` and the refresh works.

### Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000  (expects the API at :8000)
```

Set `NEXT_PUBLIC_API_BASE_URL` if the API is not on `http://localhost:8000`
(e.g. `NEXT_PUBLIC_API_BASE_URL=https://api.example.com npm run build`).

### Demo database (snapshot / restore)

`demo/skuu_demo.archive.gz` is a **1.2 MB** gzipped-BSON snapshot of the full
catalogue (`skus`: 62,647 documents) plus the restock history
(`restock_events`: 24 documents), taken from the live `skuu` database. It lets
the dashboard be demoed on a fresh machine **without re-scraping** the
storefronts and without the MongoDB Database Tools (the bundled script uses the
`bson` codec directly, so only the Python backend dependencies are needed).

```bash
cd backend

# restore into a dedicated demo database (never into the source `skuu`)
python scripts/demo_db.py import --db skuu_demo --drop

# re-create the snapshot from a database (read-only against the source)
python scripts/demo_db.py export --db skuu
```

`--drop` recreates the collections from scratch; without it the restore upserts
by `_id` (idempotent, re-runnable). The importer **refuses to `--drop` the
protected database name `skuu`** unless `--force` is passed. It recreates the
recorded indexes, including the unique `(shop, url, size)` index, so the
restored collection behaves exactly like the scraped one.

A per-collection extended-JSON alternative also exists for the `skus` collection
alone (gitignored, under `data/`):

```bash
python scripts/export_skus.py                 # -> data/skuu_skus.jsonl
python scripts/import_skus.py --db skuu_demo --drop
```

If the MongoDB Database Tools *are* installed, the equivalent classical commands
are `mongodump --db skuu --out ./dump` and
`mongorestore --db skuu_demo --drop ./dump/skuu`.

### How the snapshot is distributed

The archive is small — **1.2 MB**, far below GitHub's 100 MB per-file limit — so
the simplest reliable option is to **commit `demo/skuu_demo.archive.gz` to the
repository**: it arrives with `git clone` and Djou restores it in one command
above. (If the snapshot ever grows large, the alternatives are attaching it to a
GitHub Release and downloading it, or tracking it with Git LFS; neither is
needed at this size.)


## Scrapers

Run any shop from `backend/`, optionally with a max product count for a smoke
test. Note that `veja` defaults to **only 5 products** when no count is given,
so pass a large number for a full run (the command below is documented, not run
as part of setup):

```bash
cd backend
python -m app.scrapers.carhartt 3
python -m app.scrapers.veja 2000    # full catalog (no argument = 5-product smoke test)
python -m app.scrapers.salomon
python -m app.scrapers.asphaltgold

# Shopify retailers share one core (shopify_retailer.py):
python -m app.scrapers.afew
python -m app.scrapers.footway
python -m app.scrapers.caliroots
python -m app.scrapers.xtreme
python -m app.scrapers.beamhill
python -m app.scrapers.biffi
python -m app.scrapers.sneakersnstuff
python -m app.scrapers.nugnes1920
python -m app.scrapers.overkillshop

# SSENSE (Playwright + product sitemap; no argument = 5-product smoke test)
python -m app.scrapers.ssense
```

All scrapers reuse `carhartt.upsert_skus` (identity `(shop, url, size)`, refresh
in place) and are safe to re-run.

### Carhartt WIP implementation notes

- **Size-level extraction from the DOM.** Apparel exposes sizes as radio inputs
  under `input[name="intlSizes"]`; single-size accessories use
  `input[name="size"]`. Size text is read from `aria-label` / `value`.
- **Per-size availability.** A size is out of stock when its radio has the
  `disabled` attribute and/or its label carries the `line-through` /
  `text-midGrey` / `pointer-events-none` classes. JSON-LD only exposes a
  product-level `availability`.
- **JSON-LD fallback.** When no size selector is rendered (fully sold-out
  product), sizes come from Product JSON-LD and are recorded `in_stock = false`.
- **Full-catalog run:** 943/946 products, 4,403 SKUs (2,722 in stock), 0
  duplicate `(shop, url, size)`.

### Shopify retailer core

`backend/app/scrapers/shopify_retailer.py` implements the shared
`/products.json?limit=250&page=N` crawl (`ShopifyRetailer` config), brand
detection (Carhartt WIP / Veja / Salomon only), size-option extraction
(`Size` / Italian `Taglia`), barcode fallback (variant `barcode`, else a **bare**
all-digits GTIN-shaped `sku` — separators are never collapsed, so a compound
`STYLE-SIZE` code is not invented into a barcode), style-code capture from the
variant `sku` when it is not a GTIN (stored only — never used as a matching
identity), and per-variant stock from the `available` flag. Each retailer module
is a thin config wrapper.

### SSENSE implementation notes

`backend/app/scrapers/ssense.py` runs a headless Chromium session (reusing the
Playwright launch conventions from the Carhartt scraper) and:

- **Discovery from the product sitemap.** `robots.txt` disallows `?page=`, so
  category pages cannot be paged; the declared product sitemaps
  (`sitemap_products_list_0..4.xml`) are the complete compliant source. `loc`
  entries are filtered to the tracked designer slugs, de-duplicated and rewritten
  to the `en-de` (EUR) locale.
- **Field extraction.** Title, brand, product id and price come from the page's
  `Product` JSON-LD; the colour comes from the trailing `Supplier color: <x>` of
  the Open Graph description; per-size stock comes from the size `<select>`
  (`#size-dropdown`) — a `disabled` / `Sold Out` option is out of stock. Products
  without a size selector (accessories) fall back to the JSON-LD
  `availability` and are recorded as `One Size`.
- **Honest identity fields.** `style_code` is the JSON-LD `sku`; SSENSE publishes
  no barcode, so `barcode` is left `None` (unless the supplier code is GTIN-shaped,
  which is never the case here).
- **Fatal failures** raise a sentinel `("sitemap", …)` entry so the Refresh
  Inventory pipeline reports the shop as not refreshed rather than silently
  zero.
- **Bounded browser teardown.** Browser close / Playwright stop and each product
  attempt are capped by `CLOSE_TIMEOUT_S` / `ATTEMPT_TIMEOUT_S` so a wedged
  Chromium close cannot stall a long run (a hang would otherwise block the whole
  catalogue).
- **Full-catalogue run.** The complete tracked catalogue (402 product URLs —
  367 Carhartt WIP + 35 Veja) scrapes cleanly into **2,929 SKUs** (1,224 in
  stock), all in EUR, with 0 failures. `python -m app.scrapers.ssense [N]` runs
  the scraper (no argument = 5-product smoke test).
- **Matching note.** SSENSE prefixes the colourway in the product name and uses
  its own naming (e.g. `Blue OG Detroit Jacket`), which normalizes to a different
  `title_key` than the other shops' canonical titles, and its trousers/shoes use
  `WAIST US …` / `IT …` sizes instead of the bare numbers other shops publish.
  The matcher (engine `1.4.1`) bridges this **conservatively**: it strips the
  `WAIST US`/`IT` size prefixes and adds a `base_key` identity (the title with its
  *own* colour words removed and a trailing generic noun like `Sneakers` trimmed).
  That path is deliberately narrow — it fires only when at least one side
  genuinely begins with its colour (the SSENSE shape), the colours remain
  compatible, **and** the prefixed side's *leading colour run* is corroborated by
  the other side's structured colour (a shared accent word is not enough). On the
  live catalogue this yields **124 matched SSENSE SKUs** (SSENSE coverage
  **4.2335%**, 59 mixed-stock opportunities; **149** new cross-shop pairs
  involving SSENSE after the Prescott correction); a small, verified,
  same-product effect spills onto two non-SSENSE shops for the `OG Detroit
  Jacket` (afew omits the colour).

  Precision is good but **not perfect**, and the metrics should be read as
  best-effort rather than ground truth. The corroboration guard is conservative
  because colour vocabularies are noisy: it compares the leading colour *run*
  rather than a single first word (SSENSE writes multi-word colourways such as
  `Dusty Hamilton Brown`), so a match is still trusted when a *later* colour word
  — not the first — is the shared one, bounded by the existing subset rule. A
  colour-prefixed product whose headline colour is genuinely ambiguous against
  another shop's colour field therefore remains a theoretical false-bridge
  candidate; the guard removes the known cases without claiming zero.

## Testing

```bash
cd backend
python -m pytest tests/ -q --ignore=tests/test_database.py
```

> ⚠️ `backend/tests/test_database.py` is **not isolated** from production: it
> connects to the live MongoDB and inserts/deletes a `test_shop` row (and
> re-creates indexes) in the `skuu` database. Run it against a throwaway
> database, or exclude it when you only want the read-only checks:
> `python -m pytest tests/ -q --ignore=tests/test_database.py`.

The backend suite (**363 passed** at the time of writing) is fully isolated —
fake scrapers and in-memory collections, no MongoDB and no network. The scraper
tests run against synthetic fixtures, so they are **isolated scraper tests**, not
live-site checks. The concurrency benchmarks quoted in
[`chats/18`](chats/18_refresh_inventory_optimization.md) are **simulated,
no-write** measurements, and the only **verified real-world refresh** results are
the per-shop catalogue totals in [Shops](#shops). Do not read the simulated
numbers and the live totals as the same kind of evidence.

```bash
cd frontend
npm test        # node --test unit tests for src/lib/opps.ts and src/lib/refresh.ts
npm run lint
npm run build
```


## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Dashboard shows "Could not reach the API" | Backend not running, or on another port. Start `uvicorn app.main:app --port 8000`; if you change the port, rebuild the frontend with `NEXT_PUBLIC_API_BASE_URL` set. |
| `/api/v1/health` reports database not connected | MongoDB is not running, or `MONGODB_URI` in `backend/.env` is wrong. On Windows: `Get-Service MongoDB` / `Start-Service MongoDB`. |
| Metrics are zero / dashboard is empty | The API points at an empty database. Set `MONGODB_DB=skuu_demo` in `backend/.env` and run the demo restore (see [Demo database](#demo-database-snapshot--restore)). |
| `ModuleNotFoundError: No module named 'app'` | Run backend commands from `backend/`, not the repo root. |
| Playwright errors (`NotImplementedError`) only when refreshing | On Windows, uvicorn's `--reload`/`--workers` switch to the `SelectorEventLoop`, which cannot spawn Playwright's subprocess. Run **without** them. |
| `Executable doesn't exist … chromium` | Playwright's browser is missing: `python -m playwright install chromium`. |
| `409` from `POST /api/v1/refresh` | A refresh is already running (one job at a time). Check `GET /api/v1/refresh/status`. |
| `npm run build` fails on a fresh clone | Run `npm install` first (Node 20+). |


## MongoDB Indexes (`skus` collection)

- `shop` — single field
- `barcode` — single field
- `brand + title + colour + size` — compound, for matching
- `scraped_at` — single field
- `shop + in_stock` — compound
- `shop + url + size` — **unique**, duplicate prevention

### `restock_events` collection

Append-only log of genuine out-of-stock → in-stock transitions, written by the
Refresh Inventory pipeline. One document per SKU, so a repeated refresh updates
`last_detected_at` instead of duplicating:

- `shop + url + size` — **unique**
- `last_detected_at` — single field
- fields: `brand`, `title`, `colour`, `barcode`, `price`, `currency`,
  `first_detected_at`, `last_detected_at`

## Known limitations

- **SSENSE is scraped through a headless browser.** A plain HTTP client is
  blocked (Cloudflare 403), so it depends on a working Playwright/Chromium
  install like the Carhartt and Veja scrapers — and, because discovery uses the
  product sitemap, it only covers the two tracked designers' products rather than
  the whole site (see [Shops](#shops)). It has been run over the full tracked
  catalogue (2,929 SKUs, 0 failures).
- **SSENSE matching is partial and title-shape dependent.** SSENSE prefixes the
  colourway in the product title and uses its own naming, so its `title_key`
  rarely equals other shops' canonical titles. Engine `1.4.1` recovers the
  prefix case via a guarded `base_key` identity (see [Shops](#shops)), yielding
  124 matched SSENSE SKUs; SSENSE products whose titles do not reduce to a
  matching base key (or that lack a corroborated colour on the other side) still
  do not match, which is expected under the conservative rules. A shared *accent*
  colour word is explicitly not sufficient (the `Green Prescott Coat` /
  `Black`-only case fixed in `1.4.1`).
- **Cross-system size conversion is limited to Salomon footwear.** The Salomon
  DTC store publishes UK sizes while EU retailers publish EU sizes; a brand-scoped
  UK→EU table maps them so the DTC store now matches EU retailers. No other
  brand or size system is converted.
- **Colour is not always published** by retailers (e.g. afew, sneakersnstuff
  expose only `Size`). Where the URL handle does not clearly encode the colour,
  the colour is left empty and, if a shop lists multiple colourways, the group is
  treated as ambiguous and excluded, so those shops contribute few/no matches.
  Handle extraction recovers many (afew, biffi, nugnes1920) but not
  style-code-only handles (sneakersnstuff).
- `back_in_stock_opportunities` is a **current stock-mix signal**, not a
  historical transition. The separate `restock_events` collection records genuine
  out-of-stock → in-stock transitions, but only from the point the Refresh
  Inventory pipeline starts collecting history — a SKU whose prior state was
  never observed is treated as new, not restocked.
- **Refresh persistence is interleaved with scraping.** Each scraper upserts as
  it walks its catalogue, so the "Saving" phase reports those totals rather than
  performing a second write pass; a cancelled run therefore keeps whatever was
  already written.
- **SSENSE sitemap shards are occasionally flaky.** Individual
  `sitemap_products_list_*.xml` shards intermittently answer **503**; the scraper
  retries a bounded number of times, but a shard that stays down aborts discovery
  (reported as a fatal `sitemap` failure) rather than silently truncating the
  catalogue.

## Documentation

Development logs live in [`chats/`](chats/):

- `01_carhartt_discovery.md`, `01_carhartt_scraper.md`, `02_carhartt_scraper_debugging.md`
- `03_veja_discovery.md`, `04_veja_scraper.md`, `05_veja_scraper_debugging.md`
- `06_salomon_discovery.md`, `06_salomon_scraper.md`
- `07_asphaltgold_discovery.md`, `07_asphaltgold_scraper.md`
- `08_matching_engine.md` — cross-shop matching + metrics
- `09_retailer_expansion.md` — 8 Shopify retailers + SSENSE block
- `10_matching_validation.md` — pre-deploy false-positive validation
- `11_colour_extraction.md` — handle-derived colourways (v1.2.0)
- `12_delivery_prep.md` — delivery readiness (setup, dashboard, export/restore)
- `13_frontend_redesign.md` — premium dashboard redesign (frontend only)
- `14_retailer_completion.md` — tenth retailer (Overkill) + barcode honesty fix
- `15_refresh_inventory.md` — Refresh Inventory pipeline (progress, cancel, restocks)
- `16_ssense_scraper.md` — SSENSE headless-browser scraper (sitemap + JSON-LD)
- `17_ssense_matching.md` — SSENSE cross-shop matching fix (engine v1.4.0; leading-colour guard in v1.4.1)
- `18_refresh_inventory_optimization.md` — bounded refresh concurrency, atomic upsert and the 14→13 registry correction (engineering summary)

## Roadmap

- [x] Carhartt WIP full-catalog scraper
- [x] Additional brand storefronts (VEJA, Salomon)
- [x] Retailer scrapers (10 required retailers: 9 Shopify + SSENSE via headless browser)
- [x] Cross-shop product matching
- [x] Back-in-stock opportunity detection
- [x] Dashboard API + frontend
- [x] Refresh Inventory pipeline (progress, cancellation, restock events)
- [x] Historical stock snapshots (true restock transitions, via `restock_events`)
- [x] Colour extraction from retailer URL handles to recover ambiguous matches
