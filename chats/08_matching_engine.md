# Cross-shop matching engine + dashboard metrics

Second workstream: turn the scraped SKUs (31,594 rows across 4 shops) into the
four dashboard metrics. Pure, read-only analysis �?" the matcher never writes to
the database.

> **Superseded (see `09_retailer_expansion.md`, `10_matching_validation.md`,
> `11_colour_extraction.md`):** 8 more Shopify retailers were added (now 12
> shops / 50,302 SKUs). During pre-deploy validation two false-positive sources
> were fixed (empty-colour wildcard bridges; ambiguous multi-colourway/duplicate
> groups); handle-derived colourways then recovered more matches **without any
> regression**. The current result is **6,982 matched** (13.88% coverage), **285
> back-in-stock opportunities**, **3,388 groups**, engine `1.2.0` (was 6,812 /
> 13.54% / 242 / 3,316 on `1.1.0`). The rules in section 2 still hold; the
> numbers below are the pre-expansion baseline.

## 1. Summary

- Module: `backend/app/matching/` (`normalization.py`, `matcher.py`,
  `service.py`).
- API: `GET /api/v1/metrics`, `GET /api/v1/matching/groups` (read-only).
- UI: `frontend/src/app/page.tsx` — four KPI cards + per-shop / per-brand
  breakdown tables.
- Result on the live DB: **31,594 total**, **168 matched**, **0.53% coverage**,
  **17 back-in-stock opportunities** (82 cross-shop groups). All matches are
  Carhartt WIP between `carhartt_wip` (DTC) and `asphaltgold`.
- Tests: `backend/tests/test_matching.py` (22 tests). Full suite: **120 passed**.

## 2. What a "match" means

Two SKUs are the same product+size in **different shops** when all of:

1. **Brand** — equal and non-empty (canonical; `Carhartt` → `Carhartt WIP`).
2. **Size** — equal and non-empty after normalization (no cross-system
   conversion).
3. **Identity** — either normalized titles are equal, **or** the composite
   `normalize(title + " " + colour)` is equal. The composite rule lets a shop
   that folds colour into the title (Veja: `"ETNA SUEDE PIERRE ALMOND"`) match a
   shop that keeps colour separate (title `"Etna Suede"` + colour
   `"Pierre Almond"`).
4. **Style code** — if *both* sides carry a non-barcode style code (Carhartt
   `I037234_1`) they must be equal, else rejected.
5. **Colour** — if *both* sides have a non-empty colour, one word set must be a
   **subset** of the other. Empty colour provides no evidence (never a match
   key).

Matching is joined with union-find, blocked by `(brand, size)`, so a product in
several shops collapses to one cluster and three-shop chains work transitively.

## 3. Why the conservative rules

The colour rule was the trickiest. The first attempt used "share at least one
colour word", which produced obvious false positives driven by common accent
words:

- DTC `"Shady Blue / White"` matched retailer `"Tobacco / White"`,
  `"Black / White"`, `"Dark Scarab / White"` (all share `white`).
- `"Black / Gold"` matched `"White / Gold"` (both Chase products).

Switching to **subset-or-equal** word sets keeps the good cases (one shop adds
descriptors: `"Black"` vs `"Black Rinsed"`, `"Barchan"` vs
`"Barchan Stone Dyed"`) while rejecting the accent-word collisions
(`{black, gold}` vs `{white, gold}` — neither is a subset). Matches dropped
from 219 to 168; spot-checking the remaining groups showed all are genuine.

Style codes: the Carhartt DTC `barcode` field holds the style code, but an early
`extract_style_code` treated any value with 8–14 *digits embedded* as an
EAN/UPC and discarded codes like `I032940_89`. Fixed to only ignore codes that
are *entirely* digits.

General rules:

- Missing/empty values are **never** matching identifiers.
- No UK↔EU shoe-size conversion (Salomon is UK on DTC, EU on AsphaltGold; trying
  to map it is error-prone, so those simply don't match — 0 Salomon matches).
- Colour is compared as a word **set** with the subset rule above, not a fuzzy
  translation.

## 4. Metrics

| Metric | Definition | Live value |
| --- | --- | --- |
| `total_skus` | every SKU | 31,594 |
| `matched_skus` | SKUs in a cluster spanning ≥2 shops | 168 |
| `coverage_pct` | `matched_skus / total_skus × 100` | 0.5317 |
| `back_in_stock_opportunities` | cross-shop clusters with **mixed** stock (in stock in ≥1 shop, sold out in ≥1) | 17 |

Extras: `back_in_stock_skus` (sold-out members of those clusters, 17),
`matched_groups` (82), and `by_shop` / `by_brand` breakdowns.

### By shop (matched / coverage / opportunities)

| Shop | Total | Matched | Coverage | Back-in-stock |
| --- | ---: | ---: | ---: | ---: |
| asphaltgold | 2,587 | 86 | 3.32% | 17 |
| carhartt_wip | 4,403 | 82 | 1.86% | 17 |
| salomon | 9,923 | 0 | 0% | 0 |
| veja | 14,681 | 0 | 0% | 0 |

### Why Salomon and Veja don't match (yet)

- **Salomon**: DTC sizes are UK (`10.5 UK`), AsphaltGold EU (`42 2/3`); 20 titles
  overlap but **zero** size overlap.
- **Veja**: DTC keeps colour inside all-caps titles with an empty colour field;
  AsphaltGold uses the colour field. The composite rule helps but exact
  normalized-string agreement is rare, and AsphaltGold carries collab
  exclusives (e.g. "VEJA x Asphaltgold V-90") absent from DTC. 0 SKU matches.

## 5. API

- `GET /api/v1/metrics` → the four metrics + `by_shop` / `by_brand` +
  `algorithm_version` + `generated_at`.
- `GET /api/v1/matching/groups?limit=N` (≤500) — sample cross-shop groups with
  member details (shop, title, colour, size, price, in_stock, url).

CORS added in `backend/app/main.py` (origins from `settings.cors_origins`,
default `http://localhost:3000`).

## 6. Key files

- `backend/app/matching/normalization.py` — pure normalization helpers.
- `backend/app/matching/matcher.py` — match predicate, union-find clustering,
  `compute_metrics`, `matched_group_samples`.
- `backend/app/matching/service.py` — Mongo loader (`PROJECTION` = only needed
  fields), `get_metrics`, `get_matched_groups`.
- `backend/app/api/metrics.py` + `backend/app/api/__init__.py` — routes.
- `backend/app/main.py` — CORS.
- `backend/tests/test_matching.py` — 22 unit tests.
- `frontend/src/app/page.tsx` — dashboard (Next 16 / React 19 / Tailwind 4).
- `backend/debug_matching.py` — local report script (gitignored).

## 7. Verification

- `python -m pytest tests/ -q` → 120 passed.
- `python debug_matching.py` → metrics above; sampled groups all genuine.
- Live API smoke test (uvicorn on 8123): `/api/v1/metrics` →
  total=31594 matched=168 coverage=0.5317 opps=17; `/api/v1/matching/groups`,
  `/api/v1/health` OK.
- `npm run lint` clean; `npm run build` succeeds.

## 8. Limitations / next steps

- Coverage is small (0.53%) by design: only the Carhartt WIP overlap currently
  aligns on all keys. Salomon (size systems) and Veja (colour-in-title schema)
  need extra, carefully-validated normalization to match.
- Only one retailer is present; adding more shops carrying Salomon/Veja/Carhartt
  with matching size conventions would raise coverage.
- `back_in_stock` is currently a stock-mix signal, not a real-time transition
  (there is no historical snapshot table yet).
