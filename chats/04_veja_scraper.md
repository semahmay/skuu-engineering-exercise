# Veja Webshop — Scraper (Phase 2)

**Target:** https://www.veja-store.com/  ·  **Locale:** `en_ie` (English, Ireland, EUR)
**Module:** `backend/app/scrapers/veja.py`
**Platform:** Salesforce Commerce Cloud (SFCC / Demandware)
**Data source:** server-rendered product pages + SFCC sitemap index

---

## 1. What was built

A dedicated scraper for the Veja own-webshop that:

1. **Discovers** every product URL for one locale from the SFCC sitemap index
   (`/{locale}/sitemap_index.xml` → `*-product.xml` → `<loc>` URLs).
2. **Parses** each product page (Playwright loads it, then the raw DOM is parsed).
3. **Extracts per-size stock** from the `<li>` wrapping each size button.
4. **Persists** one SKU per product+size via the existing upsert + unique
   `(shop, url, size)` index.

Reused unchanged from `carhartt.py`: `upsert_skus`, `classify_failure`,
`accept_cookies`, and the browser retry / recovery / progress pattern. No
Carhartt file was modified.

---

## 2. Locale choice

| Storefront | Currency | Products |
|---|---|---|
| `fr_fr` (site default) | EUR | 1,033 |
| **`en_ie` (chosen)** | **EUR** | **1,033** |
| `en_gb` (separate UK store) | GBP | 626 |

`en_ie` gives an English UI with EUR pricing and the full 1,033-product catalog.
Its index lists the shared global `fr_fr/sitemap_*` product sitemaps, so URLs are
filtered to the `/en_ie/p/` prefix and de-duplicated by trailing product code.

Verified once live: 1,033 URLs / 1,033 unique codes.

---

## 3. Discovery

- `sitemap.xml` → 404 (must use `sitemap_index.xml`).
- Index → 3 product sitemaps → filter `/en_ie/p/<slug>-<CODE>.html`.
- De-duplicate by the trailing code (`extract_code`).
- Pure, unit-tested functions:
  `parse_sitemap_index`, `extract_product_urls`, `extract_code`,
  `dedupe_product_urls`, `fetch_product_urls` (httpx, bounded retries).

---

## 4. Per-size extraction (the key part)

Sizes are **buttons**, and the stock signal is on the parent `<li>`:

```html
<li class="attributes__item-option">                     <!-- in stock -->
<li class="attributes__item-option low-stock">           <!-- in stock (low) -->
<li class="attributes__item-option disabled sold-out low-stock"> <!-- OUT -->
  <button type="button" data-value="35-C" data-display-value="35"
          data-type="size" class="... desktop">35</button>
</li>
```

- `data-value` = underlying size/variant code (`35-C`, `001-C`, `115-C`).
- `data-display-value` = displayed size (`35`, `O.S`, `115`, `W8 / M7`).
- The list is rendered up to **three times** (`desktop`, `mobile`,
  `store-availability`). The scraper prefers the desktop copy and collapses
  duplicates by `data-value`.

Rule: `in_stock = "disabled" not in li_class and "sold-out" not in li_class`.

**Stored value:** `SKU.size = data-display-value`. The underlying `data-value`
code is kept internally for de-duplication and is shown in logs/reports.
JSON-LD availability is **not** used per size (it stays `InStock` even when most
sizes are sold out).

The parser is a stdlib `HTMLParser` (`_VejaDomParser`) so it is fully unit
testable without a browser.

---

## 5. Metadata / colour / price / barcode

- **JSON-LD** `Product` → `name`, `sku`/`mpn` (product code),
  `offers.price`, `offers.priceCurrency`.
- **Colour** → the selected `button[data-type="color"]` display value.
- **Barcode** → `null`: Veja exposes no GTIN/EAN/UPC anywhere.
- **DOM fallback** → some archived / "notify me" products (the **CONDOR 3**
  line) render **no JSON-LD at all**. For these, `parse_product_dom_metadata`
  reads `<h1 class="product-name">`, the analytics `"item_id"` and `"price"`
  values. If title+price are still unavailable, the page is recorded as
  `no_json_ld`.

---

## 6. Robustness

- Bounded retry: `MAX_ATTEMPTS_PER_URL = 3`.
- Browser auto-rebuild on crash/timeout (`_recreate_scrape_page`).
- **Fast-fail on permanent outcomes** (`no_json_ld`, `no_size_selector`,
  `stock_unresolved`): the site is server-rendered, so retrying a page that
  parsed but lacked data only wastes time. This removed a large amount of
  retry overhead (see `05_veja_scraper_debugging.md`).
- `_resolve_unresolved` → optional per-size AJAX fallback via the SFCC
  `Product-Variation` endpoint when a size button has no usable wrapping
  `<li>` (rare; not needed in the full run).
- Progress + final summary reused from the Carhartt pattern.

---

## 7. Results (full `en_ie` run)

| Metric | Value |
|---|---|
| Product URLs discovered | 1,033 |
| Products processed | 1,033 |
| Successful | 1,033 |
| Failed | 0 |
| 404 / skipped | 0 |
| SKUs inserted | 14,681 |
| SKU write failures | 0 |
| Unique product URLs | 1,033 |
| In stock | 8,662 |
| Out of stock | 6,019 |
| Sizes per product | min 1 / max 20 / avg 14.21 |
| Price range | 7.00 – 290.00 EUR |
| Currency | EUR (only) |
| Duplicate `(shop,url,size)` groups | 0 |
| Carhartt rows after run (untouched) | 4,403 |
| Runtime (scraper-measured) | 41,030 s (host slept repeatedly; active rate ≈ 10 s/product) |

Full-run log: `backend/veja_full_run.log` (gitignored).

### Post-run database verification

- total veja SKUs 14,681 · unique URLs 1,033
- in 8,662 / out 6,019
- duplicate `url+size` groups: **0** (unique index `uniq_shop_url_size` present)
- prices 7–290 EUR, `price == 0`: 0
- barcode null: 14,681 (100%, expected)
- empty colour: 3,330 (products with no colour swatch, e.g. totes/accessories)
- shops in DB: `carhartt_wip` 4,403 · `veja` 14,681

---

## 8. Tests

`backend/tests/test_veja_scraper.py` — 22 deterministic tests (no network/browser):

- sitemap index parse / locale filter / code extraction / de-duplication
- size de-duplication across desktop/mobile/store-availability
- stock states (in, low-stock, disabled sold-out) and missing-`<li>` = unknown
- metadata (price/currency/code), colour selection
- normal mixed stock, One Size (`O.S`/`001-C`), length sizes (`115-C`)
- all-sold-out with selector, no selector, no JSON-LD
- **DOM fallback** metadata + SKU building + `None` without price
- upsert insert / refresh / no duplicates (in-memory collection)
- partial-existing upsert

Full suite: `python -m pytest tests/ -q` → **45 passed** (23 pre-existing + 22 new).

---

## 9. Small live verification (before the full run)

| Product | Sizes | Stock |
|---|---|---|
| `campo-canvas-white-pierre-CA0103129` | 17 (35…47) | 9 in / 8 out (mixed) |
| `tote-bag-cotton-chocolat-AA0100020` | 1 (`O.S` / `001-C`) | in |
| `laces-organic-cotton-black-LA01BLA01` | 4 (115/125/135/145) | all in |
| `gt-leather-white-natural-AI0221652` | 13 | all in |
| `gt-suede-taupe-pierre-AI0321656` | 13 | all in |

All EUR, all duplicate views collapsed, per-size stock matched the live page.

---

## 10. Files

- **New:** `backend/app/scrapers/veja.py`
- **New:** `backend/tests/test_veja_scraper.py`
- **Unchanged (reused):** `backend/app/scrapers/carhartt.py`
- **Docs:** `chats/03_veja_discovery.md`, `chats/04_veja_scraper.md`,
  `chats/05_veja_scraper_debugging.md`
- Untracked/gitignored discovery + verification scripts and logs under `backend/`.

---

## 11. Known limitations

- `colour` is empty for products with no colour swatch (≈23% of rows, mostly
  accessories/totes). Veja does not expose colour in JSON-LD, so it can only be
  read from a selected swatch.
- `barcode` is always `null` (not exposed by the store).
- Sizes are stored as the displayed value; the canonical `data-value` code is
  used for identity/deduplication and is available in logs.
- Fully sold out products that render **no** size selector and **no** JSON-LD
  sizes would be recorded as `no_size_selector` (none occurred in the run).
- Runtime figure includes extended host sleep and is not a pure wall-clock.
