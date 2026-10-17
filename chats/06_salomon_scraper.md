# Salomon Official Store — Scraper (Phase 2)

**Module:** `backend/app/scrapers/salomon.py`
**Tests:** `backend/tests/test_salomon_scraper.py`
**Data source:** official Shopify storefronts — `salomon.com.au` (AUD, primary)
and `salomon.co.nz` (NZD, alternative) — public `/products.json` endpoint
**Discovery report:** `06_salomon_discovery.md`

---

## 1. Summary — why this is not the salomon.com scraper

The Phase 1 discovery report concluded that `www.salomon.com` (Adobe
Commerce / Magento PWA) **blocks all automated access** with a PerimeterX /
HUMAN challenge (`appId PX2bubKzBv`): product pages, categories, `/graphql`
and `/rest/V1/*` all return **403** to both `httpx` and plain headless
Chromium. That control is **not circumvented** (project rules forbid stealth
automation, CAPTCHA solving, proxy/fingerprint evasion).

Instead, two **official Salomon country storefronts** run on Shopify and expose
their catalog through the standard, public, unprotected `/products.json` feed:

| Store | Host | Currency | Source |
|---|---|---|---|
| Australia (chosen) | `salomon.com.au` | **AUD** | `https://salomon.com.au/products.json?limit=250&page=N` |
| New Zealand | `salomon.co.nz` | NZD | `https://salomon.co.nz/products.json?limit=250&page=N` |

These are Salomon-operated shops (same brand, same `L*` style codes as the DTC
sitemap, e.g. `L41160200`, `L49237400`), so the catalog and per-size stock are
legitimate official data — reachable without circumventing any control.

Reused unchanged from `carhartt.py`: `upsert_skus`, `classify_failure`, and the
progress/summary pattern. No Carhartt or Veja file was modified, and no schema or
matching change was needed.

---

## 2. Source endpoint & pagination

```
GET https://salomon.com.au/products.json?limit=250&page=1
GET https://salomon.com.au/products.json?limit=250&page=2
...
```

- Standard Shopify public endpoint, `200 OK`, no auth, no bot gate.
- `limit=250` is the API maximum; the scraper increments `page` until a page
  returns fewer than 250 products (the last page), with bounded retries and a
  `max_products` cutoff for sampling. A short `delay` is applied between pages.

Each product object provides: `title`, `handle`, `product_type`, `options[]`
and a `variants[]` array. Each variant provides `id`, `sku`, `price`,
`available` (bool), `barcode`, and `option1/2/3`.

---

## 3. Store choice

| Store | Currency | Notes |
|---|---|---|
| **`salomon.com.au` (AU, chosen)** | **AUD** | English, full AU catalog, `products.json` clean |
| `salomon.co.nz` (NZ) | NZD | English, similar catalog; selectable via `--store=nz` |

`run_scraper(..., store_key=...)` selects the storefront; AU is the default.

---

## 4. Field mapping

| SKU field | Source | Notes |
|---|---|---|
| `shop` | constant | `"salomon"` |
| `brand` | constant | `"Salomon"` |
| `title` | `product.title` | |
| `colour` | option `Color`/`Colour` value | `""` when the product has no colour option |
| `size` | option `Size`/`Shoe size` value | `"One Size"` when there is no size option |
| `price` | `variant.price` (string) | parsed to `float`; `0.0` if absent/unparseable |
| `currency` | per store | `AUD` (AU) / `NZD` (NZ) |
| `barcode` | `variant.barcode` | always `null` on Salomon — stored as-is |
| `in_stock` | `variant.available` (bool) | **authoritative per-size stock from the source** |
| `url` | `{base}/products/{handle}?variant={id}` | unique per size/colour variant |
| `scraped_at` | scrape time | |

Real option names confirmed live on page 1 (250 products): `Size` (248),
`Color` (166), `Colour` (84). Both spellings are handled.

---

## 5. Extraction logic (pure functions)

- `find_option_index(product, names)` — case-insensitive match of an option name
  against a set (`{"size","shoe size"}` or `{"color","colour"}`); returns the
  option index or `None`. Shape-agnostic (handles both `Color` and `Colour`,
  and either option order).
- `extract_size` / `extract_colour` — read `option{index+1}` from the variant.
  Missing size → `"One Size"`; missing colour → `""`.
- `variant_url(store, handle, variant_id)` — builds the per-variant URL used as
  the SKU identity, so two colours of the same product/size are distinct rows.
- `build_sku(product, variant, store)` — assembles one SKU. **Stock is never
  guessed:** if `available` is not a real `bool` (missing, `null`, `1`,
  `"true"`, ...) the variant is **skipped**, not defaulted. Missing `handle`
  also skips.
- `parse_products(products, store)` → `(skus, skipped)`.

---

## 6. Persistence

One SKU per product+size+colour, written via `upsert_skus` against the unique
`(shop, url, size)` index `uniq_shop_url_size`. Re-running refreshes
stock/price in place and creates no duplicates. Progress is printed every 250
products; a final summary reports products, variants, skipped variants, new vs
refreshed SKUs, DB totals, in/out stock counts and runtime.

---

## 7. Running

```bash
# sample: 10 products (CLI default) from the AU storefront
python -m app.scrapers.salomon

# explicit sample size
python -m app.scrapers.salomon 25

# alternative storefront (New Zealand)
python -c "import asyncio; from app.scrapers.salomon import run_scraper; asyncio.run(run_scraper(10, store_key='nz'))"
```

The CLI positional argument is a **sample cap**. To process the whole paginated
catalog, call the scraper with `max_products=None`:

```python
import asyncio
from app.scrapers.salomon import run_scraper
asyncio.run(run_scraper(None, store_key="au"))   # full AU catalog
```

---

## 8. Tests

`backend/tests/test_salomon_scraper.py` — no network, no DB:

- option discovery (`Color`/`Colour`, `Size`/`Shoe size`, absent option, order);
- size/colour extraction incl. `One Size` and empty colour;
- variant URL identity (with and without variant id);
- `build_sku` field mapping, per-store currency;
- **stock:** `True`/`False` map correctly; non-bool `available`
  (`None`, `1`, `0`, `"true"`, `""`) and missing key are **skipped**, never
  guessed; invalid price → `0.0`; missing barcode → `null`; missing handle skips;
- `parse_products` counts skipped variants and yields distinct URLs for two
  colours of the same size;
- pagination stops on a short page and at the `limit`;
- upsert inserts once then refreshes without creating a duplicate row.

---

## 9. Blocked DTC routes (documented, not bypassed)

The following `www.salomon.com` routes return `403` PerimeterX (`appId PX2bubKzBv`)
to automated clients and were **not** circumvented:

| Route | Result |
|---|---|
| `/en-*/product/...` (all locales) | 403 PX (httpx, headless Chromium) |
| `/en-*/c/...` category pages | 403 PX |
| `POST /graphql` | 403 PX |
| `/rest/V1/...` | 403 PX |
| `/product-export`, `/media/feeds/...` | 404 / 403 |
| `/api/sitemap/products?locale=XX` | **200** (allow-listed; URLs only) |

The DTC sitemap yields URLs + `lastmod` only — **no price/stock/GTIN** — so it
cannot supply SKUs on its own. That is exactly why the official AU/NZ Shopify
storefronts are used instead. Other candidate hosts were unusable
(`salomon.ca` SSL mismatch; `salomon.us`/`salomon.co.za` 404;
`salomon.ie`/`salomon.co.uk` DNS fail; `salomon.eu` TLS fail).

---

## 10. Verification

### 10.1 Sample run (15 products) — pre-full-run gate

- Run: `python -m app.scrapers.salomon 15` → 15 products, 0 failed, 134 variants
  (0 skipped), 134 SKUs inserted, in-stock 131 / out 3.
- Re-run of the same sample: **0 inserted, 134 refreshed**, total still 134 (no
  duplicates). Deep source comparison: 0 field mismatches.
- Carhartt (4,403) and Veja (14,681) unchanged.

### 10.2 Full AU catalog run

Command: `run_scraper(None, store_key="au")` (whole paginated `products.json`).

| Metric | Actual |
|---|---:|
| Products discovered / processed | 1,583 / 1,583 |
| Products failed | **0** |
| Variants seen | 9,923 |
| Variants with unknown stock (skipped) | 0 |
| New SKUs inserted | 9,789 |
| Existing SKUs refreshed | 134 |
| SKU write failures | 0 |
| Total Salomon SKUs in DB | 9,923 |
| Unique product URLs | 9,923 |
| In stock | 5,360 |
| Out of stock | 4,563 |
| Runtime | 0m 41s |

### 10.3 Post-run MongoDB verification (direct)

| Check | Actual |
|---|---|
| Salomon SKU records | 9,923 |
| Distinct product URLs | 9,923 |
| Duplicate `(shop, url, size)` groups | **0** |
| In stock / out of stock | 5,360 / 4,563 |
| Empty titles / empty URLs | 0 / 0 |
| Prices ≤ 0 | 0 |
| Currencies | `{"AUD": 9923}` |
| Barcode null | 9,923 / 9,923 |
| Source comparison (1,583 products → 9,923 SKUs) | missing 0, extra 0, **field mismatches 0** |
| `uniq_shop_url_size` index | present |
| Carhartt / Veja records | 4,403 / 14,681 (unchanged) |
| Total collection docs | 29,007 |

**Interruption safety:** the unique `(shop, url, size)` index plus the
`find_one` → `update_one`/`insert_one` upsert means a re-run never duplicates;
the sample re-run confirmed 134 rows refreshed in place.

**Tests:** `python -m pytest tests/ -q` → **72 passed** (45 pre-existing +
27 Salomon).

### 10.4 Data-quality observations (not scraper defects)

- **7 records have empty `colour`** — products whose source variant has no
  `Color`/`Colour` option (`STEEP SKI POLES`; single-colour `SHIFT ALPHA BOA 115
  WOMEN'S`, 6 sizes). Correct: there is no colour value to read.
- **Size strings are the source's literal values**, which are not consistently
  normalised upstream (e.g. `10.5 UK` vs `10.5  UK`, `3.5 UK` vs `3.5 Uk`,
  `4UK`, `One Size`/`OSFA`, `NS`/`N/S`, binding/width codes such as `145W`).
  Stored verbatim; no rows were lost or merged.
- **`barcode` is null for all 9,923 rows** — the Salomon Shopify source never
  populates it.
- **No failed products, and no missing/invalid titles, URLs, sizes, prices or
  currencies.**

---

## 11. Limitations / notes

- Source is the **AU/NZ** storefront, so prices are **AUD** (AU run) / NZD (not
  run). The AU catalog (1,583 products / 9,923 variants) is what is ingested.
- Real DTC `www.salomon.com` product pages/API remain blocked by PerimeterX and
  were **not** circumvented (see §9); no DTC-locale mapping is attempted.
- Size/colour strings are stored exactly as the source provides them (see
  §10.4); downstream matching must tolerate the upstream formatting.
- Barcode/GTIN is absent from this source, so any cross-store matching for
  Salomon must rely on title/size/colour (or the Salomon style code, which is in
  `variant.sku` and is **not** currently persisted).

---

## Appendix — files

- `backend/app/scrapers/salomon.py` — scraper (this phase)
- `backend/tests/test_salomon_scraper.py` — 27 unit tests
- `chats/06_salomon_discovery.md` — Phase 1 discovery (DTC block findings)
- `backend/salomon_discover_*.py` — discovery/validation helpers (gitignored),
  incl. `salomon_discover_validate.py` (baseline / `verify N` / `quality`)
- `backend/salomon_discover_full.py` — full-catalog runner (`run_scraper(None)`)
- `backend/salomon_full_run.log`, `backend/salomon_full_result.json` — full-run
  log and machine-readable summary (gitignored)
- `backend/salomon_*.xml`, `salomon_block_page.html` — discovery artifacts (gitignored)
