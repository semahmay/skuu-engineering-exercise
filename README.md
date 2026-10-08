# Product Inventory Matching System

An inventory-intelligence system for fashion retail. It collects product
inventory from online stores, extracts product- and size-level information,
detects per-size stock availability, normalizes SKUs, matches the same product
across retailers, and identifies back-in-stock opportunities. All inventory is
stored in MongoDB.

> **SKU definition:** one SKU = one product + one size + one shop.
> A six-size product therefore produces six SKU records.

## Features

- **Inventory collection** from e-commerce storefronts (public pages, headless Chromium).
- **Size-level extraction** from the product DOM, not just aggregated product data.
- **Per-size stock detection**, including partially sold-out products.
- **JSON-LD fallback** for products whose size selector is not rendered.
- **SKU normalization** into a single document shape per size.
- **Cross-retailer matching** scaffolding (same product across shops).
- **Back-in-stock discovery** from historical stock snapshots.
- **MongoDB storage** with idempotent upserts and duplicate protection.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Scraping | Python, Playwright (headless Chromium) |
| API | FastAPI, Pydantic |
| Database | MongoDB (Motor async driver) |
| Frontend | Next.js, React, TypeScript, Tailwind CSS |
| Testing | pytest, pytest-asyncio |

## Repository Layout

```
.
├── backend/                 # FastAPI service, scrapers, data models, tests
│   ├── app/
│   │   ├── main.py          # FastAPI app + lifespan
│   │   ├── config.py        # Settings (environment variables)
│   │   ├── api/             # HTTP routes (health, ...)
│   │   ├── database/        # Motor client, SKU repository, indexes
│   │   ├── matching/        # Cross-retailer matching (scaffolding)
│   │   ├── models/          # Pydantic SKU models
│   │   └── scrapers/        # Per-shop scrapers (Carhartt WIP)
│   ├── tests/               # pytest suite
│   ├── check_db.py          # Inspect stored SKUs
│   ├── check_duplicates.py  # Verify no duplicate (shop, url, size) records
│   ├── verify_full_run.py   # Post-run catalog verification
│   ├── requirements.txt
│   └── .env.example
├── frontend/                # Next.js dashboard
└── chats/                   # AI-assisted development logs
```

## Getting Started

### Backend

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
cp .env.example .env          # then edit values
```

Environment variables (see `.env.example`):

```
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=skuu
APP_ENV=development
API_HOST=0.0.0.0
API_PORT=8000
```

Run the API:

```bash
cd backend
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
# Health: GET http://localhost:8000/api/v1/health
```

### Frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000
```

## Testing

```bash
cd backend
python -m pytest tests/ -q
```

## Carhartt WIP Scraper

The first implemented shop is **Carhartt WIP** (`carhartt-wip.com`, `en-de`
locale). Product URLs are discovered from the store's XML sitemap.

### Usage

```bash
cd backend
python -m app.scrapers.carhartt 3      # small run (first 3 products)
python -m app.scrapers.carhartt 946    # full catalog
```

Progress is printed per product, with periodic aggregate stats
(processed / successful / failed / inserted / updated / elapsed / ETA) and a
final summary including any failed URLs and their reasons.

### Implementation Notes

- **Size-level extraction from the DOM.** Apparel exposes sizes as radio
  inputs under `input[name="intlSizes"]`; single-size accessories use
  `input[name="size"]`. Size text is read from `aria-label` / `value`.
- **Per-size availability.** A size is out of stock when its radio has the
  `disabled` attribute and/or its label carries the
  `line-through` / `text-midGrey` / `pointer-events-none` classes. This is the
  only reliable source of *per-size* stock; JSON-LD only exposes a
  product-level `availability`.
- **JSON-LD fallback.** When no size selector is rendered at all (fully
  sold-out product), sizes are taken from the Product JSON-LD and recorded
  with `in_stock = false`. JSON-LD is also the source for title, colour,
  price, currency and barcode.
- **MongoDB upsert.** The identity of a SKU is `(shop, url, size)`. New
  identities are inserted; existing identities are refreshed in place
  (stock, price, title, `scraped_at`). Re-running is safe and resumable.
- **Duplicate protection.** A unique MongoDB index on `(shop, url, size)`
  makes duplicates impossible at the storage layer.
- **Bounded retries & recovery.** Each URL is attempted at most 3 times.
  Navigations are classified (`http_404`, other non-200, `no_json_ld`,
  timeout, page crash). A crashed/closing page triggers a full Playwright
  stack rebuild, and one bad URL never aborts the run.
- **Polite scraping.** Public pages only, headless Chromium, no login, no
  CAPTCHA/Cloudflare bypass, no stealth plugins, no proxies, no fingerprint
  spoofing. Heavy resources (images/fonts/media/stylesheets) are blocked and a
  fixed delay separates requests; concurrency is kept at one.

### Full Catalog Run — Results

| Metric | Value |
|--------|-------|
| Product URLs discovered | 946 |
| Successfully processed | 943 |
| Failed | 3 (`no_json_ld`) |
| 404 / skipped | 0 |
| Total SKUs in database | 4,403 |
| New SKUs inserted | 3,498 |
| Existing SKUs refreshed | 1,419 |
| Unique product URLs | 943 |
| In stock | 2,722 |
| Out of stock | 1,681 |
| Products with mixed per-size stock | 363 |
| Duplicate `(shop, url, size)` groups | 0 |
| Tests passing | 23 |
| Runtime | ~7h 11m |

## MongoDB Indexes (`skus` collection)

- `shop` — single field
- `barcode` — single field
- `brand + title + colour + size` — compound, for matching
- `scraped_at` — single field
- `shop + in_stock` — compound
- `shop + url + size` — **unique**, duplicate prevention

## Documentation

Development logs live in [`chats/`](chats/):

- `01_carhartt_discovery.md` — site inspection, selectors, data structures
- `01_carhartt_scraper.md` — initial implementation log
- `02_carhartt_scraper_debugging.md` — stock-detection bug analysis and full-catalog run

## Roadmap

- [x] Carhartt WIP full-catalog scraper
- [ ] Additional brand storefronts
- [ ] Retailer scrapers (assorted marketplaces) for the same brands
- [ ] Cross-retailer product matching
- [ ] Back-in-stock detection and alerting
- [ ] Dashboard API endpoints and frontend integration
