# Veja Scraper — Debugging Log

Issues hit while building and running the Veja scraper, and how each was fixed.

---

## 1. `sitemap.xml` is 404 — use `sitemap_index.xml`

SFCC does not serve `sitemap.xml`. The real index is `sitemap_index.xml`, which
lists `sitemap_0..2-product.xml` plus image/category/content sitemaps. The
scraper fetches the index, keeps sitemaps whose URL contains `product`, then
parses their `<loc>` entries.

## 2. Locale filtering / duplicate URLs

The `en_ie` index points at the **shared global** `fr_fr/sitemap_*` product
sitemaps, which contain **all 97 locales** for the same products. Fix: keep only
URLs matching `/en_ie/p/...html` and de-duplicate by the trailing product code.

## 3. Wrong stock signal (button vs parent `<li>`)

Initial inspection looked at the `<button>` classes and saw identical classes
for in- and out-of-stock sizes. The real signal is on the **parent `<li>`**:
`disabled` / `sold-out` mean out of stock; `low-stock` alone still means in
stock. Implemented via an `HTMLParser` that tracks the `<li>` class stack.

## 4. Sizes rendered three times

Each size appears in `desktop`, `mobile` and `store-availability` copies. The
scraper prefers the desktop copy and de-duplicates by `data-value`, reporting how
many duplicates were collapsed (e.g. Campo: 17).

## 5. JSON-LD availability is not per size

`offers.availability` stayed `InStock` even when 11/18 sizes were sold out. It is
therefore ignored for stock; per-size stock comes exclusively from the DOM.

## 6. `extract_code` returned `""` for `.../CA0103129.html`

The first regex required a `-` before the code. Sitemap URLs always end with
`-<CODE>.html`, but the unit fixture `.../CA0103129.html` (no dash) failed. Added
a fallback that takes the last path token before `.html` (and strips a leading
dash/slug). Cosmetic input robustness; the sitemap path was already correct.

## 7. `dedupe_product_urls` kept later duplicates

Bug: when a code was already seen the code did `continue`, but the first branch
also appended before marking `seen`. Rewrote so a URL with a seen code is
skipped *before* appending; URLs without a code are always kept.

## 8. Fake collection missing `_id` (test-only)

`upsert_skus` reads `existing["_id"]` from `find_one(..., {"_id": 1})`. The
in-memory test collection returned docs without `_id`, so updates raised
`KeyError` → reported as failures. Fix: the fake assigns an auto-increment
`_id` on insert (mirrors real Mongo).

## 9. Duplicate counting after desktop filtering

`extract_size_options` filtered to the desktop copy *before* counting, so the
duplicate count was always 0. Reordered to `desktop + others`, then de-duplicate,
so the collapsed mobile/store-availability copies are counted.

## 10. Deterministic `no_json_ld` caused huge retry overhead  ← main issue

**Symptom:** the first full run slowed down and reported 28+ failures, all
`no_json_ld`, clustered on the **CONDOR 3** product line.

**Diagnosis:** those pages render **no JSON-LD** (and no `og:type=product`), but
they *do* contain the full size DOM. The retry loop treated `no_json_ld` as
retryable, so every CONDOR page was fetched 3× with growing back-off
(~15–35 s each), drastically slowing the run for no benefit — the page is
server-rendered and deterministic.

**Fix (two parts):**

1. **DOM fallback** — `parse_product_dom_metadata` reads the title from
   `<h1 class="product-name">`, the product code from the analytics `"item_id"`,
   and the price from the analytics `"price"` value (currency defaults to EUR).
   Verified on a CONDOR 3 page: title/price/code all present, plus 40 size
   buttons. Covered by tests.
2. **Fast-fail on permanent outcomes** — `PERMANENT_REASONS` (`no_json_ld`,
   `no_size_selector`, `stock_unresolved`) break the retry loop immediately.
   Only crash/timeout/transient-HTTP errors retry with browser rebuild.

**Result:** re-ran the full catalog → **1,033/1,033 successful, 0 failures**
(the DOM fallback recovered the entire CONDOR 3 line).

## 11. Clean re-run after fixes

The improved code was applied and the partial Veja data deleted
(`delete_many({"shop": "veja"})`, 5,607 rows) so the final run reports clean
insert counts. Carhartt data (`carhartt_wip`, 4,403 rows) was left untouched.

---

## Result

`python -m pytest tests/ -q` → **45 passed**.
Full run → 1,033 products, 0 failures, 14,681 SKUs (8,662 in / 6,019 out),
0 duplicate `(shop,url,size)` groups, all EUR, prices 7–290.
