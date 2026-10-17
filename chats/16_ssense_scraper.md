# 16 — SSENSE scraper (headless browser + product sitemap)

Date: 2026-10-15

## Objective

SSENSE (`ssense.com`) was the one shop left out of the pipeline: a documented
stub (`STATUS="blocked"`) because a plain HTTP client always got **Cloudflare
403**. Turn it into a working, tested, integrated scraper — covering the two
brands the project tracks (Carhartt WIP, Veja) — **without** bypassing any
access control, or else produce an evidence-backed blocker.

## 1. Access model (why the browser is enough)

- A plain `httpx` GET to `/en-us/men`, product pages and `sitemap.xml` returns
  **403** with `server: cloudflare`, `cf-mitigated: challenge` and a
  "Just a moment…" interstitial — a normal bot check, not a hard block.
- A regular **headless Chromium** session (Playwright, the same launch setup the
  Carhartt scraper already uses) loads the same URLs with **HTTP 200** and no
  interstitial: the page's own JavaScript runs and the Cloudflare bot cookies
  (`__cf_ob`, `__cf_bm`) are issued in the normal course of navigation.
- No CAPTCHA solving, proxy, fingerprint spoofing, stolen/external cookies or new
  third-party service — just a browser visiting a public page.

## 2. Locale / currency

Product URLs are locale-tagged (`/en-us/...`). The scraper rewrites the locale
segment to **`en-de`** so prices are **EUR**, matching every other shop in the
project, and `/en-de` is permitted by `robots.txt`.

## 3. Discovery (robots-compliant)

`robots.txt` disallows `?page=`, so category pages cannot be paged. The declared
product sitemaps (`sitemap_products_list_0..4.xml`) are the complete compliant
source. Full discovery over all five shards found **401** in-scope product URLs
(**366** `carhartt-work-in-progress` + **35** `veja`); by the time of the full
scrape the live sitemap listed **402** (**367** + **35**). SSENSE does **not**
carry Salomon (no designer page, zero slugs). Shards occasionally answer **503**
and are retried a bounded number of times; persistent failure is reported as a
fatal `("sitemap", …)` sentinel.

## 4. Extraction

- `Product` JSON-LD → title, brand, product id, `offers.price`,
  `offers.priceCurrency`, `offers.availability`.
- `<link rel=canonical>` → stable product URL.
- Colour → trailing `Supplier color: <x>` in the Open Graph description.
- Per-size stock → the size `<select id="size-dropdown">`; `disabled` /
  `Sold Out` options are out of stock. Products with no size selector
  (accessories) fall back to the JSON-LD availability and are stored as
  **`One Size`**.
- `style_code` = JSON-LD `sku`; SSENSE publishes no barcode, so `barcode` stays
  `None` (never fabricated).

## 5. Integration

- `backend/app/refresh/shops.py` — registered
  `ShopSpec("ssense", "SSENSE", "retailer", "app.scrapers.ssense")`; removed the
  "SSENSE intentionally absent" note and corrected the `stats_to_totals` /
  `scraper_failed` docstrings.
- `frontend/src/app/page.tsx` — added `ssense: "SSENSE"` to `SHOP_LABELS`.
- `backend/tests/test_shopify_retailer.py` — removed the SSENSE import and the
  obsolete `test_ssense_documented_blocked` (SSENSE is no longer a Shopify
  retailer nor a blocked no-op).

## 6. Live runs and verification

**Full-catalogue scrape (live).** `python -m app.scrapers.ssense [N]` runs the
scraper (no argument = 5-product smoke test; the `__main__` block was fixed to
call `run_scraper`, not the read-only `probe()`). Across the whole tracked
catalogue (**402** product URLs) the scraper persisted **2,929 SKUs** (1,224 in
stock / 1,705 out) with **0 failures and 0 404s**, all in EUR on `/en-de` URLs,
all carrying a `style_code`, none carrying a fabricated `barcode`. The run was
completed in two stages (a first pass that reached 298/402, then a resume that
processed the remaining 104 URLs); a resume helper reuses `run_scraper`
unchanged, feeding it only the URLs missing from the DB.

**Robustness fixes found during the run.** The initial long run degraded and
then hung. Two issues were fixed in `ssense.py`:

- the resource-blocking route handler (`_block_heavy_resources`) now swallows the
  benign `Invalid InterceptionId` race that occurs when a request is in flight as
  the page navigates (`route.continue_()` could otherwise raise inside the event
  loop, slowing the run and forcing needless browser rebuilds);
- browser teardown and each attempt are now bounded (`CLOSE_TIMEOUT_S`,
  `LAUNCH_TIMEOUT_S`, `ATTEMPT_TIMEOUT_S`) so a wedged Chromium close / launch can
  no longer stall the whole run.

**Post-run DB verification (read-only).** `global_total = 62,647` =
`59,718` (non-SSENSE, unchanged) + `2,929` (SSENSE); 13 distinct shops; all
2,929 rows `currency=EUR`, `/en-de/`; `barcode` never set; `colour`/`size` never
empty; brands only Carhartt WIP (2,455 SKUs / 367 URLs) and Veja (474 / 35).

**Matching / dashboard verification (read-only).** SSENSE is loaded into the
matcher automatically and appears in `by_shop` with its stock. On the first
integration it produced **0 cross-shop matches**. Root cause, diagnosed on the
live data: SSENSE titles prefix the colourway and use SSENSE's own naming (e.g.
`Blue OG Detroit Jacket` -> title key `blue og detroit jacket`) so **no** SSENSE
SKU shared a `title_key` / `composite_key` with another shop in the same (brand,
size) block, and its trousers used `WAIST US 28` rather than `W29`/`W29L32`.

This was then fixed in a dedicated matching workstream (matching engine
`1.4.0`, see [`17_ssense_matching.md`](17_ssense_matching.md)): a guarded
`base_key` identity (title minus its own colour words, trailing generic noun
trimmed) plus `WAIST US`/`IT` size-prefix stripping first yielded **130 matched
SSENSE SKUs** (SSENSE coverage 4.4384%, 61 mixed-stock opportunities) — these
**1.4.0** figures are historical. A post-implementation precision audit then
found a **Prescott Coat false positive**, and engine **`1.4.1`** (the current
version) added a **leading-colour corroboration guard** (see §8 of that doc); the
verified current figures are **124 matched SSENSE SKUs** (SSENSE coverage
**4.2335%**, **59** mixed-stock opportunities). No `restock_events` are created
by a first import (only the Refresh pipeline records transitions); `ssense`
has 0.

**Automated tests.**

```
cd backend
python -m pytest tests/ -q --ignore=tests/test_database.py    # 272 passed (scraper-time; suite since grown — see 17)
```

New module `backend/tests/test_ssense_scraper.py` covers the pure helpers
(`product_slug`, `canonical_brand`, `rewrite_locale`, `parse_sitemap_locs`,
`parse_colour`, `parse_size_options`, `build_skus`), the one-size fallback, the
fatal-sitemap propagation in discovery, and the refresh-registry registration.

## Outstanding

- SSENSE depends on a working Playwright/Chromium install and (on Windows) an
  origin event loop — start the server **without** `--reload` (see README).
- SSENSE cross-shop matching was closed by engine `1.4.0`, then corrected to
  engine **`1.4.1`** (current; see §6 above and
  [`17_ssense_matching.md`](17_ssense_matching.md)): **124** matched SKUs via a
  guarded `base_key` identity + `WAIST US`/`IT` size-prefix normalization and the
  `1.4.1` leading-colour corroboration guard, with no loosening of the
  conservative colour rules. (The original `1.4.0` run matched 130 — historical.)
  Products whose titles do not reduce to a matching base key still do not match
  (expected).
