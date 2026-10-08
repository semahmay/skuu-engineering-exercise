# Carhartt WIP Scraper — Stock Bug Debugging Log

## The Bug

The original scraper stored `in_stock` for all sizes of a product as identical
(all `True` or all `False`). The old DB state proved this: of 905 SKUs there were
zero products with mixed per-size stock — impossible for a real catalog.

## Root Cause

Two compounding problems:

1. **Wrong selector.** The scraper looked for size radios with `name="id"`.
   The actual product page uses `input[name="intlSizes"]` for apparel
   (and `input[name="size"]` for One Size accessories). The old selector
   matched nothing, so per-size availability was never observed.

2. **Stock inferred from JSON-LD.** JSON-LD (`schema.org/Product`) lists *all*
   sizes and only a product-level `offers.availability`. It cannot express
   "size XS sold out, S–XXL in stock". No embedded variant/stock JSON exists
   in page scripts either — the DOM is the only source of truth.

The old "click each size, check Add-to-cart disabled" approach was unreliable
and slow; it was removed.

## DOM Structure (verified live)

Test page: `.../p/og-arcan-jacket-black-black-destroy-wash-303`

```
<label class="... cursor-pointer">            <- grandparent carries stock classes
  <span class="...">
    <input type="radio" name="intlSizes"
           value="XS" aria-label="XS" id="I036867_25" disabled>  <- disabled = sold out
  </span>
</label>
```

- **Available size**: radio has no `disabled`; label lacks stock classes.
- **Sold out**: radio has `disabled` AND label class contains
  `line-through text-midGrey pointer-events-none`.
- **Size text**: `aria-label` (more reliable than `value`).
- **One Size products** (e.g. backpack): single radio `name="size"`,
  `value="0"`, **no `id`**, no `aria-label`. JSON-LD `size: ["0"]`.
  Early version skipped these because it required a non-empty `id` —
  fixed by dropping the `id` requirement.
- **Fully sold out product**: no size selector rendered at all →
  fall back to JSON-LD sizes with `in_stock=False`.

## Fixes Applied (`backend/app/scrapers/carhartt.py`)

| Change | Detail |
|--------|--------|
| `SizeVariant` dataclass | size, variant_id, in_stock |
| `get_size_variants(page)` | reads `input[name="intlSizes"]`, falls back to `input[name="size"]`; stock from `disabled` attr + label classes |
| `scrape_product_page` | uses DOM variants as the size source; no selector → JSON-LD sizes with `in_stock=False`; numeric size `0` → `"One Size"` |
| `sku_exists(shop, url, size)` + `save_skus` | skip before insert; identity = `(shop, url, size)`; no post-hoc dedup needed |

## Verification

### 3-case live test (`test_small_verification.py`)

| Product | Result | Runtime |
|---------|--------|---------|
| OG Arcan Jacket (mixed) | 6 SKUs: XS `False`, S/M/L/XL/XXL `True` | ~6s |
| Kickflip Backpack (One Size) | 1 SKU: One Size `True` | ~5s |
| OG Double Knee Pant (mixed) | 6 SKUs: XS/S `True`, M/L/XL/XXL `False` | ~4s |

(The "OG Double Knee Pant — fully in stock" expectation in the test label was
wrong; the page really is mixed, and the scraper reports it correctly.)

### Unit tests (`pytest tests/ -v`) — 23 passed

Cover: JSON-LD parsing, mixed/all-in/One-Size/sold-out variants, label-class
fallback without `disabled`, no-selector → all `False`, numeric→"One Size",
non-200 → empty, `sku_exists` true/false, `save_skus` first run inserts /
second run skips / partial existing.

### Duplicate prevention (live, one product)

```
CLEANUP: deleted 6 existing records
RUN 1: scraped=6, inserted=6, skipped=0   -> 6 records
RUN 2: scraped=6, inserted=0, skipped=6   -> 6 records
DUPLICATE shop+url+size groups: none
```

## Limitations

- Stock is a point-in-time snapshot; no re-check scheduling.
- `en-de` locale only; some sitemap URLs 404 (skipped by error handling).
- Rapid sequential loads can trigger `Page crashed` — resource blocking used,
  1.5s delay between products retained.

---

## Full Catalog Run

Run date: 2026-10-08. Command: `python -u -m app.scrapers.carhartt 946`,
launched as a detached background process writing to `backend/full_run.log`.
Resumable by design: identity is `(shop, url, size)` and existing rows are
refreshed in place, so a re-start never duplicates and never re-scrapes wasted.

### Pre-run changes (no change to working stock logic)

- Added bounded retry: `MAX_ATTEMPTS_PER_URL = 3` (1 try + max 2 retries).
  A page crash / timeout rebuilds the whole Playwright stack (`_recreate_scrape_page`).
- Non-200 classified: `http_404` counted as "Skipped/404"; other non-200,
  crash, timeout, or missing JSON-LD (`no_json_ld`) counted as failed.
  A single bad URL never terminates the run.
- Progress line every 25 products: `processed | ok | failed | 404 | new |
  updated | elapsed | eta`.
- `upsert_skus`: new identities inserted; existing identities refreshed in
  place (stock/price/title/scraped_at). One record per identity, always.
- Added unique MongoDB index `uniq_shop_url_size` on `(shop, url, size)`
  as structural duplicate protection.

### REAL results

```
Product URLs discovered: 946
Products processed:      946
Successful:              943
Failed:                  3
Skipped/404:             0
Total SKUs in database:  4403
New SKUs inserted:       3498
Existing SKUs refreshed: 1419
SKU write failures:      0
Unique product URLs:     943
Unique product titles:   935
In stock:                2722
Out of stock:            1681
Runtime:                 7h11m (25,899 s)
```

Note on the starting state: before this run the DB held 905 rows across 193
URLs from the old buggy scraper (wrong stock flags). Those 193 URLs were
refreshed in place; 3498 genuinely new rows were inserted; total 4403.

### Failed URLs (3, all `no_json_ld` after 3 attempts)

```
https://www.carhartt-wip.com/en-de/p/w-shepton-og-michigan-coat-black-natural-972
https://www.carhartt-wip.com/en-de/p/snake-camo-compact-umbrella-snake-camo-237
https://www.carhartt-wip.com/en-de/p/w-s-s-torion-t-shirt-black-grind-washed-796
```

### Duplicate + integrity check (`verify_full_run.py`)

```
Duplicate (shop,url,size) groups: 0
Products with MIXED per-size stock: 363   (was 0 with the buggy scraper)
Indexes: _id_, shop_1, barcode_1, brand_1_title_1_colour_1_size_1,
         scraped_at_1, shop_1_in_stock_1, uniq_shop_url_size
```

The 363 mixed-stock products are the key evidence the per-size stock logic
works at catalog scale (the old scraper produced exactly zero).

### Runtime notes / resilience

- ~16 transient timeouts and 1 long stall (~90 min on a single URL) were
  observed; all were recovered by the bounded-retry + browser-rebuild path.
- Eventually Failed = 3 (0.3%), all `no_json_ld` (page served without a
  parseable Product JSON-LD block). No 404s in the final run.
- 3 failed URLs have no rows in the DB, so Unique product URLs = 946 - 3 = 943.

### Known limitations (this phase)

- 3 products not represented due to missing JSON-LD; left un-modified.
- Stock is a snapshot at scrape time; no scheduling/refresh.
- `en-de` locale only.
- Single-threaded, ~1.5s politeness delay; correctness favoured over speed.

