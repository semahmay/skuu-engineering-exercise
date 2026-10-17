# AsphaltGold — discovery (retailer selection)

Goal: add **one real retailer** that sells at least one existing brand
(Carhartt WIP / Veja / Salomon) so the same products exist in more than one shop
and cross-shop matching becomes possible. All three original shops are
single-brand DTC stores, so before this step the cross-shop overlap was **0 by
construction** (see the matching audit).

## Constraints applied

- Public pages / public endpoints only. No CAPTCHA solving, no stealth/headless
  evasion, no proxy or fingerprint spoofing, no auth bypass.
- Must not touch `carhartt.py`, `veja.py`, `salomon.py`.
- Prefer a Shopify `/products.json` storefront (proven with Salomon).

## Candidate screening

### 1. Platform check (`/products.json?limit=250&page=1`)

| Candidate | Shopify `/products.json` |
|---|---|
| ssense.com | 404 — not Shopify |
| sneakersnstuff.com | 200 |
| afew-store.com | 200 |
| **asphaltgold.com** | **200** |
| caliroots.com | 200 |
| biffi.com / footway.com / beamhill.fi / nugnes1920.com | reachable (mixed) |

`ssense.com` rejected (not Shopify). "Xtreme" (mentioned in project notes) has
**no** discovery notes anywhere in the repo and could not be verified → skipped.

### 2. Brand presence (`search/suggest.json` and brand collections)

| Store | Salomon | Veja | Carhartt WIP |
|---|---|---|---|
| asphaltgold | yes | yes | yes |
| afew | yes | yes | yes |
| caliroots | only 5 Salomon | yes | yes |
| biffi | small | small | yes |
| sneakersnstuff | yes | yes | yes |

### 2b. AsphaltGold brand collections (`/collections/{handle}/products.json`)

| Collection | Products | Variants | Options |
|---|---:|---:|---|
| `salomon` | 81 | 1,654 | Size, Color |
| `veja` | 28 | 266 | Size, Color |
| `carhartt-wip` | 125 | 594 | Size, Color |

Full-catalog vendor filter (19 pages, 4,525 products) is more complete than any
single collection and is what the scraper uses:

| Brand (`vendor`) | Products |
|---|---:|
| Salomon | 82 |
| Veja | 29 |
| Carhartt WIP | 135 |
| **Total overlapping** | **246** |

### 3. Variant shape (live sample)

```
options: ['Size', 'Color']            # order varies per product -> match by NAME
variant: option1 '42 2/3'  option2 'Paloma / Cloudburst'
         sku 'salomon260071-42-2-3'  price '209.00'  available True  barcode None
```

- colour is an explicit option (not buried in the title);
- `available` is a real per-size bool;
- `sku` present, `barcode` null for all sampled.

## Decision

**AsphaltGold (https://www.asphaltgold.com)** — Shopify storefront, EUR.

Reasons: stocks **all three** brands; public, unprotected `/products.json`;
per-variant `available` and explicit `Size`+`Color`; SKU present; no anti-bot
gate; small, bounded overlapping set (246 products / 2,587 variants).

Mapping: all 246 overlapping products use `Size` + `Color` options, every
variant has a bool `available`, and no variant was skipped.

Note on the catalog: Shopify `/products.json` is capped at **250 per page**, so
discovery paginates until a short page (19 pages here). All `/products.json`
requests are public and unauthenticated; only a small polite delay is used
between pages.
