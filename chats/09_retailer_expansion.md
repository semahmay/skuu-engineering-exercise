# Retailer expansion — 8 Shopify stores + SSENSE (blocked)

Third workstream: widen coverage by scraping the same three brands (Carhartt WIP,
Veja, Salomon) from nine additional retailers, then re-run the matcher over the
bigger dataset. Eight retailers are Shopify stores with a public
`/products.json` feed; SSENSE is Cloudflare-protected and could not be scraped.

> **Note:** the matching metrics below (`1.1.0`) were later improved to
> **6,982 matched / 13.88% / 285 opportunities** by conservative URL-handle
> colour extraction — see `11_colour_extraction.md`.

## 1. Summary

| Shop | Platform | Currency | Products | SKUs | In stock | Crawl mode |
| --- | --- | --- | ---: | ---: | ---: | --- |
| sneakersnstuff | Shopify | EUR | 84 | 884 | 267 | collections + full scan |
| afew | Shopify | EUR | 363 | 4,280 | 1,637 | collections + full scan |
| footway | Shopify | SEK | 748 | 4,092 | 1,977 | collections + full scan |
| xtreme | Shopify | EUR | 680 | 2,879 | 2,074 | collections + full scan |
| biffi | Shopify | EUR | 85 | 465 | 230 | collections + full scan |
| beamhill | Shopify | EUR | 284 | 1,411 | 828 | collections + full scan |
| caliroots | Shopify | SEK | 571 | 3,983 | 1,591 | collections + full scan |
| nugnes1920 | Shopify | EUR | 174 | 714 | 403 | full scan only |
| **SSENSE** | proprietary | EUR | 0 | 0 | 0 | **blocked (Cloudflare 403)** |

New rows: **18,708** (all variants), bringing the collection to **50,302 SKUs**
across **12 shops** (`carhartt_wip`, `veja`, `salomon`, `asphaltgold` + the 8
above). No duplicates on `(shop, url, size)`, no empty title/size/url, and every
variant has an authoritative `in_stock` value from the feed (no unknown-stock
rows).

## 2. Shared Shopify core

All eight accessible retailers share one implementation,
`backend/app/scrapers/shopify_retailer.py`, driven by a small per-shop config:

```python
ShopifyRetailer(
    shop_id="afew",
    base_url="https://www.afew-store.com",
    currency="EUR",
    use_collections=True,   # try /collections/<brand>/products.json first
    full_scan=True,         # also page the whole catalog (brand detection)
    max_pages=None,
)
```

- `detect_brand(title, vendor)` maps a product to Carhartt WIP / Veja / Salomon
  via vendor + title tokens; anything else is skipped (scope is 3 brands).
- `find_option_index` / `extract_size` read the variant's `Size` option (Biffi
  uses the Italian `Taglia`, handled in `SIZE_OPTION_NAMES`).
- `extract_colour` reads an explicit colour option when present; these stores
  mostly fold colour into the title and expose only `Size`, so `colour` is left
  **empty** (honest — never invented).
- `extract_barcode` uses the variant `barcode` when it is a real EAN/UPC
  (entirely digits); Beamhill puts the barcode in the variant `sku`, handled by
  fallback.
- `variant_url` builds a stable `?variant=<id>` URL so the unique
  `(shop, url, size)` index works per size.
- `run_scraper_for` reuses `carhartt.upsert_skus` (refresh in place, no
  duplicates) and `carhartt.classify_failure`.

Each thin module (`afew.py`, `footway.py`, …) only declares its config and a
`run_scraper()` entry point; `python -m app.scrapers.<shop> [max_products]`
works like the existing scrapers. `nugnes1920` disables collection mode
(`use_collections=False, full_scan=True`) because it has no `carhartt-wip` /
`veja` / `salomon` collection handles.

## 3. SSENSE

`backend/app/scrapers/ssense.py` is a documented **blocked** stub
(`STATUS="blocked"`, `REASON="cloudflare_403_challenge"`). All catalog routes
(`/products.json`, `/collections.json`, search suggest, product pages) return
HTTP 403 with `server: cloudflare` behind a "Just a moment…" JS challenge, and
SSENSE is not a Shopify store anyway. Bypassing anti-bot is out of scope, so
`run_scraper()` is a no-op returning zero SKUs and `probe()` reproduces the
block.

## 4. Matching-engine update (important)

The shared core stores `colour=""` for most new SKUs. The original matcher
treated an empty colour as a wildcard, and because clustering was a plain
union-find over compatible pairs, an empty-colour shop **bridged** two genuinely
different colourways (e.g. a colourless retailer merged Carhartt `Black` with
asphaltgold `Stone Canvas`). Fix in `matcher.py`:

- Identity matching (brand, size, title/composite, style) is unioned first,
  **ignoring colour** (`_identity_match`).
- Each identity group is then split into colourways (`_split_by_colour`):
  - colour-bearing members are grouped into **pairwise-compatible** groups
    (clique, not connected components) so the non-transitive subset relation can
    not bridge colourways (e.g. `Black` / `Stone Canvas` / `Black Stone Canvas`);
  - no colour group → empty members form one group;
  - one colour group → it absorbs the empty members;
  - several colour groups → each stands alone and empty members form their own
    colour-unknown group (never used to merge two colourways).

Result: **0** clusters contain a mutually incompatible colour pair (was 159
bridged clusters before the fix).

## 5. Metrics over the expanded dataset

| Metric | Before (4 shops) | After (12 shops) |
| --- | ---: | ---: |
| `total_skus` | 31,594 | 50,302 |
| `matched_skus` | 168 | 10,230 → **6,812** after validation |
| `coverage_pct` | 0.5317 | 20.3372 → **13.5422** after validation |
| `back_in_stock_opportunities` | 17 | 601 → **242** after validation |
| `back_in_stock_skus` | 17 | 1,588 → **323** after validation |
| `matched_groups` | 82 | 3,980 → **3,316** after validation |

The "after validation" column is the result once two false-positive sources were
removed (see `10_matching_validation.md`); the raw expansion numbers shown here
were inflated by empty-colour bridging and ambiguous colourway merges. High
coverage is now concentrated in `caliroots` (70.4%) and `footway` (70.4%), which
share colour-in-title titles and size conventions. Salomon DTC still contributes
**0** matches (UK vs EU sizes). Engine `algorithm_version` is now `1.1.0`.

## 6. Tests / verification

- New: `backend/tests/test_shopify_retailer.py` (39 tests) — extraction, brand
  detection, barcode fallback, per-variant stock, per-shop configs, SSENSE block.
- `backend/tests/test_matching.py` grew to **25 tests**, adding coverage for the
  colour-bridge fix (empty colour absorbs a single colourway; empty colour does
  not bridge two colourways; non-transitive subsets stay apart).
- Full suite: **162 passed**.
- Fixed a Windows test-infra bug: `test_ssense_documented_blocked` used
  `asyncio.run`, which unsets the current event loop and broke later async tests
  (the session `event_loop` fixture in `conftest.py` then had no loop). Converted
  to `@pytest.mark.asyncio`.
- Live API smoke over the full dataset: `/api/v1/metrics` → 200, total=50302,
  matched=10230, coverage=20.3372, opps=601, 12 shops, 3 brands;
  `/api/v1/matching/groups?limit=2` → 200.

## 7. Key files

- `backend/app/scrapers/shopify_retailer.py` — shared Shopify core.
- `backend/app/scrapers/{sneakersnstuff,afew,footway,xtreme,biffi,beamhill,caliroots,nugnes1920}.py`
  — per-shop configs.
- `backend/app/scrapers/ssense.py` — documented blocked stub.
- `backend/app/matching/matcher.py` — `_identity_match`, `_split_by_colour`,
  colour-aware `build_clusters`.
- `backend/tests/test_shopify_retailer.py`, `backend/tests/test_matching.py`.
- `backend/debug_verify_retailers.py`, `backend/debug_inspect_clusters.py`,
  `backend/debug_api_smoke.py` — local verification scripts (gitignored).

## 8. Limitations / next steps

- Most new shops don't expose colour separately, so many retailer matches are
  title+size only (documented, not invented). Extracting colour from titles
  reliably would raise precision further.
- Cross-system shoe sizes are still not converted, so Salomon DTC matches
  nothing; a validated UK↔EU map would unlock that.
- `back_in_stock` remains a stock-mix signal, not a time-series transition.
