# AsphaltGold scraper (retailer)

Second workstream first retailer added so the same products exist in multiple
shops. See `chats/07_asphaltgold_discovery.md` for how it was chosen.

## 1. Summary

- Retailer: **AsphaltGold** (https://www.asphaltgold.com), Shopify storefront,
  **EUR**.
- Source: public, unprotected `https://www.asphaltgold.com/products.json?limit=250&page=N`.
- Scope: ingest **only** products whose Shopify `vendor` is one of our existing
  brands — `Salomon`, `Veja`, `Carhartt WIP` (`OVERLAP_BRANDS`). The rest of the
  catalog (Adidas, Nike, …) is ignored.
- `shop = "asphaltgold"`; `brand` is taken from the product's `vendor`.
- Reuses `upsert_skus` / `classify_failure` from `carhartt.py`; the Carhartt,
  Veja and Salomon scrapers are **not modified**.

## 2. Why this retailer

First *retailer* (not DTC) so the same SKU can appear in two shops — the
precondition for matching. It stocks all three brands, exposes clean per-size
stock and explicit colour, and needs no anti-bot workaround. Full catalog is
small (4,525 products / 19 pages), of which **246** are our brands.

## 3. Discovery

`fetch_products()` paginates `/products.json` (`limit=250`, `MAX_PAGES=80`
safety cap, short page stops the loop, `PAGE_DELAY_S=0.4` between pages) and
keeps only products whose `vendor` is in `OVERLAP_BRANDS`. At selection time the
full catalog was 19 pages / 4,525 products → 246 matching.

## 4. Field mapping

| SKU field | Source | Notes |
|---|---|---|
| `shop` | constant | `"asphaltgold"` |
| `brand` | `product.vendor` | only `Salomon`/`Veja`/`Carhartt WIP` are kept |
| `title` | `product.title` | e.g. `Salomon XT-4 OG GTX` |
| `colour` | option `Color`/`Colour` value | matched by **name** (order varies); `""` if absent |
| `size` | option `Size`/`Shoe size` value | `"One Size"` if absent |
| `price` | `variant.price` (string) | parsed to `float`; `0.0` if unparseable |
| `currency` | constant | `EUR` |
| `barcode` | `variant.barcode` | `null` on this source — stored as-is |
| `in_stock` | `variant.available` (**bool**) | authoritative per-size stock; non-bool ⇒ variant skipped, never guessed |
| `url` | `{BASE_URL}/products/{handle}?variant={id}` | unique per variant |
| `scraped_at` | scrape time | |

## 5. Extraction logic (pure functions)

- `find_option_index(product, names)` — case-insensitive option-name match;
  handles either option order (`Size, Color` and `Color, Size` both occur).
- `extract_size` / `extract_colour` — read `option{index+1}`; missing size →
  `"One Size"`, missing colour → `""`.
- `variant_url(handle, variant_id)` — per-variant URL = SKU identity.
- `build_sku(product, variant)` — assembles one SKU; **returns `None`** when
  `available` is not a real `bool` or `handle` is empty (stock never guessed).
- `parse_products(products)` — returns `(skus, skipped_variants)`.
- `is_overlap_brand(product)` — vendor filter.

## 6. Running

```powershell
# sample of 15 overlapping-brand products (CLI arg is a sample cap; default 10)
python -m app.scrapers.asphaltgold 15

# whole overlapping-brand catalog (246 products)
python asphaltgold_full_run.py      # calls run_scraper(None); gitignored
```

The CLI positional argument is a **sample cap**; full catalog requires
`run_scraper(None)`.

## 7. Tests

`backend/tests/test_asphaltgold_scraper.py` — no network, no DB (26 tests):

- `is_overlap_brand` accepts our three brands, rejects others/`None`;
- option discovery in both orders + `Colour`/`Shoe size` spellings, absent option;
- size/colour extraction incl. `One Size` and empty colour;
- per-variant URL identity;
- `build_sku` field mapping (brand from `vendor`, EUR, barcode `null`),
  `True`/`False` stock, non-bool `available` (`None`, `"true"`, `1`, `0`, `"yes"`)
  **skipped**, missing handle skips, invalid price → `0.0`;
- `parse_products` distinct URLs per colour and skipped counting;
- pagination **and brand filtering** (fake `_get_json`);
- upsert inserts once then refreshes without duplicating the `(shop, url, size)`
  row.

`python -m pytest tests/ -q` → **98 passed** (72 pre-existing + 26 AsphaltGold).

## 8. Verification

### 8.1 Sample run (15 products)

`python -m app.scrapers.asphaltgold 15` → 15 products, 159 SKUs (0 skipped,
0 failed), 159 inserted, in 47 / out 112, 0m25s.

### 8.2 Full overlapping-brand catalog run

Command: `run_scraper(None)` (whole paginated catalog, vendor-filtered).

| Metric | Actual |
|---|---:|
| Products discovered / processed | 246 / 246 |
| Products failed | **0** |
| Variants seen | 2,587 |
| Variants with unknown stock (skipped) | 0 |
| New SKUs inserted | 2,428 |
| Existing SKUs refreshed | 159 |
| SKU write failures | 0 |
| Total AsphaltGold SKUs in DB | 2,587 |
| Unique product URLs | 2,587 |
| In stock | 1,355 |
| Out of stock | 1,232 |
| Runtime | 0m 33s |

### 8.3 Post-run MongoDB verification (direct)

| Check | Actual |
|---|---|
| AsphaltGold SKU records | 2,587 |
| Distinct product URLs | 2,587 |
| Duplicate `(shop, url, size)` groups | **0** |
| Empty titles / URLs / sizes / colours | 0 / 0 / 0 / 0 |
| Prices ≤ 0 | 0 |
| Currencies | `{"EUR": 2587}` |
| Barcode non-null | 0 |
| Brands | `Carhartt WIP`, `Salomon`, `Veja` |
| Distinct product handles | 246 |
| Source comparison (246 products → 2,587 SKUs) | missing 0, extra 0, **field mismatches 0** |
| Carhartt / Veja / Salomon records | 4,403 / 14,681 / 9,923 (unchanged) |
| Total collection docs | 31,594 |

### 8.4 Matching payoff (sanity check, not the matching engine)

Exact brand-prefix-stripped title matches against the same brand's DTC store:

| Brand | AsphaltGold products | DTC products | exact-title matches |
|---|---:|---:|---:|
| Salomon | 39 | 846 | **20** |
| Carhartt WIP | 84 | 935 | **56** |
| Veja | 16 | 1,004 | 0 |

The retailer therefore creates the multi-shop overlap that was previously
impossible. (Veja store titles differ from DTC naming; matching for Veja will
need normalisation — out of scope here.)

## 9. Limitations / notes

- Barcodes are absent on this source, so matching must use title/size/colour
  (the retailer `sku` contains a style code, e.g. `salomon260071-42-2-3`, but it
  is **not** currently persisted and is not a GTIN).
- Sizes are literal source values (EU shoe sizing `42 2/3`, apparel `S/M/L`).
- Prices are EUR; the original DTC stores are AUD/GBP/EUR, so matching must be
  cross-currency aware.
- Only the three overlapping brands are ingested; everything else in the store
  is intentionally ignored.

## Appendix — files

- `backend/app/scrapers/asphaltgold.py` — scraper
- `backend/tests/test_asphaltgold_scraper.py` — 26 unit tests
- `chats/07_asphaltgold_discovery.md` — retailer selection / discovery
- `backend/asphaltgold_full_run.py` — full-catalog runner (`run_scraper(None)`)
- `backend/asphaltgold_verify.py` — DB-vs-source verification helper
- `backend/asphaltgold_full_run.log`, `backend/asphaltgold_full_result.json` —
  full-run log + machine-readable summary (gitignored)
