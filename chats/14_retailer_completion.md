# 14 — Retailer completion (10th retailer) + barcode honesty fix

Date: 2026-10-12

## Objective

Close the two confirmed gaps left after the matching work (v1.3.1):

1. The exercise asks for **10 international retailers**. Only **9** were
   contributing data; `ssense` is blocked (Cloudflare 403) and cannot be
   completed ethically.
2. The shared Shopify core could **fabricate a barcode** from a compound
   `STYLE-SIZE` variant SKU — a violation of "represent missing information
   honestly, not fabricated".

## 1. Barcode extraction no longer fabricates GTINs

**Bug.** `extract_barcode` removed spaces/hyphens from `variant.sku` *before*
checking whether the result was a GTIN. A store that stores a compound
`STYLE-SIZE` code (e.g. Overkill's `114805-37`) therefore had its SKU collapsed
to `11480537` and stored as an 8-digit "barcode" that the source never
published.

```
# before
sku='114805-37'  -> barcode='11480537'   (fabricated EAN-8)
```

**Fix.** Added `_gtin_shaped(sku)` in
`backend/app/scrapers/shopify_retailer.py`: a SKU is a GTIN only when it is a
**bare** all-digits code of length 8/12/13/14 — separators are **not** collapsed.
Both `extract_barcode` (fallback) and `extract_variant_style_code` now share it,
so a compound SKU is stored as a `style_code` and never leaks into `barcode`.

- Real GTIN `4064958104131` → still a barcode (unchanged).
- Compound `114805-37` → `barcode = None`, `style_code = "114805-37"`.

**No matching impact.** `barcode` is not a matching identity; the matcher's
`extract_style_code(barcode)` returns `""` for all-digits values and for `None`
alike, so this change is metric-neutral. Measured across the 8 existing
shared-core retailers (≈13.5k variants, read-only) **0** variants change
behaviour — the bug was latent, not corrupting live data.

## 2. Tenth accessible retailer: `overkillshop`

SSENSE is not a Shopify store and is Cloudflare-gated, so it stays a documented
stub. Candidates were probed read-only via `/products.json`; Overkill (Berlin)
was selected over alternatives (Slam Jam, Antonioli) because it exposes
`carhartt-wip` / `salomon` / `veja` **brand collections** and clear `Size`
options, so it uses the efficient collection crawl rather than a full scan.

- `backend/app/scrapers/overkillshop.py` — thin `ShopifyRetailer` config
  (`shop_id="overkillshop"`, `https://www.overkillshop.com`, EUR,
  `use_collections=True`), re-exporting the shared entry points.
- Variant `sku` values are compound (`114805-37`, `117977-40`); with the fix
  above they are stored as `style_code` with `barcode=None`.
- With Overkill, **10 international retailers** now target the system
  (AsphaltGold + 9 shared-core Shopify retailers).

## 3. Dashboard label clarity (frontend only)

`frontend/src/app/page.tsx`:

- The three brand DTC shops are now labelled `"<Brand> (brand store)"` so a shop
  label is never identical to the brand name it sells.
- The header counts **retailers** (shops excluding the 3 brand stores) via
  `retailerCount()`, instead of labelling all 12 shops as "retailers".

## Verification

Read-only / mocked only (no live scrape, no live DB writes):

```
cd backend
python -m pytest tests/test_shopify_retailer.py tests/test_matching.py -q   # 123 passed
python -m pytest tests/ -q --ignore=tests/test_database.py                  # 217 passed
python -m pytest tests/test_database.py --collect-only -q                   # 4 collected (not run)
cd frontend
npm test && npm run lint && npm run build
```

Full suite including the 4 live-DB tests = **221**. README updated accordingly.

Regression tests added (`backend/tests/test_shopify_retailer.py`):

- `test_barcode_does_not_fabricate_gtin_from_compound_sku`
- `test_style_code_keeps_compound_sku`
- `test_build_sku_compound_sku_is_style_code_not_barcode`
- `overkillshop` added to `SHOP_MODULES` / `EXPECTED_SHOPS` / default-discovery.

## Outstanding (requires a live scrape — not run here)

- First run of `python -m app.scrapers.overkillshop` to populate its inventory.
  Until then live metrics are unchanged (Overkill contributes 0 rows) and the
  README Shops table marks its SKUs as "first run pending".
