# Carhartt WIP Scraper — Implementation Log

## Overview
Built a production-ready Playwright scraper for Carhartt WIP (https://carhartt-wip.com) that extracts product data at the SKU level (one record per product + size combination) and stores in MongoDB.

---

## Discovery Phase (Documented in `chats/01_carhartt_discovery.md`)

### Key Findings
- **No Cloudflare/bot protection** — only a simple cookie consent banner ("Accept all")
- **Sitemap-based discovery** — 946 products in `en-de` locale at `/en-de/product/sitemap.xml`
- **JSON-LD (schema.org/Product)** — Primary data source for title, color, price, barcode, all sizes, aggregated stock
- **HTML (JS-rendered)** — Required for per-size stock via radio button interaction
- **Product URL pattern**: `/en-de/p/{handle}-{id}`
- **Sizes**: Radio buttons with `name="id"` and variant IDs as values

---

## Implementation Decisions

### 1. Architecture
```
backend/app/scrapers/carhartt.py
├── run_scraper(max_products)           # Main entry point
├── fetch_sitemap_urls(limit)           # Discovery via sitemap
├── scrape_product_page(url)            # Per-product extraction
│   ├── parse_json_ld()                 # Base product data
│   ├── get_size_variants()             # Radio button variant IDs
│   ├── match_sizes_to_variants()       # Map sizes → variant IDs
│   └── check_size_stock()              # Click + check "Add to cart"
└── save_skus()                         # Upsert to MongoDB (dedupe by shop+url+size)
```

### 2. Data Extraction Strategy
| Field | Source | Notes |
|-------|--------|-------|
| `title`, `color`, `price`, `currency`, `barcode`, `sizes[]` | JSON-LD | Stable, no JS needed |
| `in_stock` (per size) | HTML + JS | Click each size radio → check "Add to cart" disabled state |
| `url` | JSON-LD `offers.url` | Canonical URL |
| `shop` | Constant | `"carhartt_wip"` |
| `brand` | Constant | `"Carhartt WIP"` |

### 3. Size Mapping Logic
```python
def match_sizes_to_variants(sizes, variants):
    if not variants:                    # No radios (accessories)
        return [{"size": s, "variant_id": None} for s in sizes]
    if len(sizes) == len(variants):     # 1:1 by order
        return zip(sizes, variants)
    # Fallback: label similarity matching
    ...
```
- **Accessories** (caps, bags): No size radios → `"One Size"` with JSON-LD stock
- **Apparel**: Radio buttons present → click each, check button state

### 4. Deduplication
- Unique key: `(shop, url, size)`
- Prevents re-insertion on re-runs
- Verified: 2nd run of 3 products → 11 skipped, 0 inserted

### 5. Politeness & Reliability
- **Resource blocking**: Images, fonts, stylesheets, media aborted
- **Delays**: 1.5s between products, 500ms between size selections
- **Error handling**: Try/except per product; log and continue
- **Headless Chromium**: No visible browser

---

## Problems Encountered & Fixes

### 1. Browser Crashes on Product Pages
**Symptom**: `Target crashed` / `Page crashed` when navigating to product URLs
**Cause**: Heavy JavaScript / resource loading
**Fix**: Block `image`, `font`, `media`, `stylesheet` resource types via `page.route()`

### 2. Cookie Consent Blocking Content
**Symptom**: French cookie banner ("Confidentialité des données") covering page
**Fix**: Auto-click "Accept all" button on first page load (`accept_cookies()`)

### 3. Size "0" for Accessories
**Symptom**: First product (Skye Cap) returned size `"0"` instead of meaningful label
**Cause**: JSON-LD `size` array empty or numeric; radio buttons absent
**Fix**: Detect empty/numeric size → map to `"One Size"`; skip variant click

### 4. Deduplication Only by URL
**Symptom**: Only first size per product inserted; subsequent sizes skipped
**Cause**: `find_sku_by_shop_and_url()` didn't include size in query
**Fix**: Added `sku_exists(shop, url, size)` checking all three fields

### 5. Price Encoding Issues
**Symptom**: `€` rendered as `�` in console logs
**Cause**: Windows console encoding (CP1252) vs UTF-8
**Fix**: Data stored correctly in MongoDB (verified: 49.0, 55.0, 249.0); logging artifact only

### 6. Test Failures Due to Implementation Mismatch
**Issue**: Unit tests assumed label-based size matching was primary
**Reality**: Order-based 1:1 mapping is primary (more reliable)
**Fix**: Updated tests to match actual behavior; added fallback test

---

## Final Scraper Behavior (Verified)

### Run 1 (3 products, empty DB):
```
Products discovered: 3
Products scraped:    3
SKUs inserted:       11
  - Skye Cap:                    1 SKU (One Size)
  - Women's L/S Blackletter Tee: 4 SKUs (XS, S, M, L)
  - OG Arcan Jacket:             6 SKUs (XS, S, M, L, XL, XXL)
SKUs skipped:        0
Errors:              0
```

### Run 2 (same 3 products, DB populated):
```
Products discovered: 3
Products scraped:    3
SKUs inserted:       0
SKUs skipped:        11
Errors:              0
```

### Database State (11 SKUs):
```
carhartt_wip | Skye Cap | One Size | 49.0   | True
carhartt_wip | Women's L/S Blackletter Waffle T-Shirt | XS | 55.0 | True
carhartt_wip | Women's L/S Blackletter Waffle T-Shirt | S  | 55.0 | True
carhartt_wip | Women's L/S Blackletter Waffle T-Shirt | M  | 55.0 | True
carhartt_wip | Women's L/S Blackletter Waffle T-Shirt | L  | 55.0 | True
carhartt_wip | OG Arcan Jacket | XS  | 249.0 | True
carhartt_wip | OG Arcan Jacket | S   | 249.0 | True
carhartt_wip | OG Arcan Jacket | M   | 249.0 | True
carhartt_wip | OG Arcan Jacket | L   | 249.0 | True
carhartt_wip | OG Arcan Jacket | XL  | 249.0 | True
carhartt_wip | OG Arcan Jacket | XXL | 249.0 | True
```

---

## Test Coverage
| Test File | Tests | Coverage |
|-----------|-------|----------|
| `test_database.py` | 4 | MongoDB connection, indexes, CRUD |
| `test_health.py` | 1 | Health endpoint |
| `test_carhartt_scraper.py` | 9 | JSON-LD parsing, size matching, edge cases |

**Total**: 14 tests passing

---

## Commands

### Run Scraper (3 products for verification):
```bash
cd backend
python -m app.scrapers.carhartt 3
```

### Run Full Scraper (all 946 products):
```bash
cd backend
python -m app.scrapers.carhartt 946
```

### Run Tests:
```bash
cd backend
python -m pytest tests/ -v
```

### Check Database:
```bash
cd backend
python check_db.py
```

---

## Limitations & Future Work

1. **Single locale (en-de)** — Parameterize locale for multi-country
2. **Per-size stock requires JS click** — Slow (~2s per size); could explore `__NEXT_DATA__` parsing for variant stock
3. **No incremental scrape** — Add `lastmod` comparison from sitemap
4. **No retry/backoff** — Add exponential backoff for failed requests
5. **No monitoring** — Add metrics (products/sec, error rate, SKU counts)
6. **Variant ID ↔ JSON-LD SKU mapping** — Not fully understood; `_15` suffix may encode size
7. **Out-of-stock detection** — Only tested on in-stock items; need to verify OOS button behavior

---

## Files Created/Modified
```
backend/
├── app/
│   └── scrapers/
│       └── carhartt.py          # Main scraper (351 lines)
├── tests/
│   ├── test_carhartt_scraper.py # Unit tests (9 tests)
│   ├── test_database.py         # Existing (4 tests)
│   └── test_health.py           # Existing (1 test)
├── requirements.txt             # Added playwright
└── check_db.py                  # Debug script

chats/
├── 01_carhartt_discovery.md     # Discovery documentation
└── 01_carhartt_scraper.md       # This file
```

---

## Next Steps (Not Implemented)
- [ ] Scale to full 946 products
- [ ] Add other 12 shops
- [ ] Build matching logic (same product across shops)
- [ ] Create dashboard API endpoints
- [ ] Connect frontend to real data