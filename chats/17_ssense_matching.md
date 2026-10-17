# 17 — SSENSE cross-shop matching (engine 1.4.0 → 1.4.1)

Date: 2026-10-15

> **Current engine version: `1.4.1`.** Sections §1–§7 document the original
> `1.4.0` workstream and are kept as the historical record (their figures are
> explicitly labelled); §8 documents the `1.4.1` Prescott false-positive
> correction and the current figures.

## Objective

After SSENSE was scraped into the catalogue (see
[`16_ssense_scraper.md`](16_ssense_scraper.md)) its 2,929 SKUs produced **zero
cross-shop matches**: every matchable SSENSE SKU was invisible to the rest of
the engine. Fix that *without* lowering precision — recover genuinely equivalent
products while keeping every existing (already-validated) match and not
inventing matches to inflate coverage.

## 1. Root cause (live data)

`build_clusters` compares two SKUs only when they share a **blocking bucket** —
`title_key` or `composite_key` — inside the same `(brand, size)` block. On the
live dataset **no** SSENSE SKU shared either key with another shop:

- **Colour-prefixed titles.** SSENSE folds the colourway into the title as a
  leading word (`Black OG Active Jacket` -> `black og active jacket`) while other
  shops keep it in the colour field (`OG Active Jacket` + `Black` ->
  `og active jacket`). The keys can never be equal.
- **Size systems.** SSENSE trousers/shoes carry explicit prefixes
  (`WAIST US 28 x 32` -> `waistus28x32`; `IT 40` -> `it40`) while every other
  shop publishes the bare number (`28/32` -> `2832`; `40` -> `40`).

## 2. Rule design (measured, not guessed)

Four candidate rules were simulated over the live DB (new **SKU-pairs** vs the
current engine, which is the true "behaviour change" metric — not cluster
signatures, which over-count any cluster whose membership merely grows):

| Variant | base identity | SSENSE clusters | new SSENSE pairs | pure non-SSENSE pairs | V-90 | Simple Pant |
|---|---|---:|---:|---:|:--:|:--:|
| A | strip own colour anywhere | 130 | 155 | 19 (incl. a false positive) | yes | yes |
| B | strip leading colour run only | 120 | 145 | 6 | **no** | yes |
| **C** | **strip anywhere + gate on a colour-prefixed side** | **130** | **155** | **6** | **yes** | **yes** |
| D | leading-only + gate | 120 | 145 | 6 | no | yes |

- **A** recovered the important VEJA V-90 case but created a false positive:
  veja `V-12 LEATHER WHITE` (`White`) matched asphaltgold `V-12 Leather`
  (`Extra White / Camel`) — different colourways that both reduce to
  `v 12 leather`.
- **B** avoided that but **lost V-90**, because the Veja DTC folds the colour
  into the *trailing* title (`V-90 LEATHER WHITE BLACK`), which a leading-only
  strip cannot reach.
- **C** keeps A's recall and kills A's false positive by requiring that at least
  one side actually **begins with its own colour word** (the SSENSE shape) before
  the colour-stripped keys are allowed to bridge.

Chosen: **variant C**.

### The implemented rule

1. `base_key` = `title_key` with the SKU's **own** colour words removed, then a
   trailing generic noun (`sneakers`/`shoe`/`shoes`/`trainers`/`jeans`) trimmed.
   Only colour words *this SKU actually has* are removed (never the whole colour
   vocabulary, which contains product words like `archives`, `bag`, `bert`).
2. A third `_same_product` path: `base_key` equal **and** at least one side
   `colour_prefixed` **and** colours compatible. `base_key` is also added as a
   blocking bucket.
3. `normalize_size` strips the SSENSE `WAIST US …x…` / `WAIST US …` / `IT …`
   prefixes (unique to SSENSE, so no other shop is touched).

Colour rules, the empty-colour-not-a-wildcard rule, the subset/clique colour
split and cross-shop restriction are **unchanged**.

## 3. Implementation

- `backend/app/matching/normalization.py` — new `base_product_key`,
  `is_colour_prefixed`, `GENERIC_PRODUCT_TAIL`; `normalize_size` gained the
  `_SSENSE_WAIST`/`_SSENSE_WAIST_SINGLE`/`_SSENSE_IT` prefixes.
- `backend/app/matching/matcher.py` — `NormalizedSKU` gained `base_key` and
  `colour_prefixed`; `normalize_sku` computes them; `_same_product` gained the
  guarded base-key path; `build_clusters` adds the `base_key` bucket;
  `ALGORITHM_VERSION = 1.4.0` (later bumped to `1.4.1` — see §8); module docstring
  updated.

## 4. Tests (synthetic units only — no DB, no network)

`backend/tests/test_matching.py` grew from 61 to **74** tests. New coverage:

- `normalize_size` `WAIST US`/`IT` prefixes (incl. `WAIST US 28 x 32 == 28/32`,
  and distinct sizes never collapse);
- `base_product_key` / `is_colour_prefixed` units;
- positive: colour-prefixed title vs plain title same colour; multi-word colour
  subset; generic-tail `Sneakers` trim (VEJA V-90); `WAIST US`/`IT` size bridge;
  3-shop clustering through SSENSE;
- negative (precision guards): prefixed title with a different colour rejected;
  prefixed title with a colourless plain side rejected; **non**-prefixed titles
  never use `base_key` (the V-12 false positive is pinned as rejected); different
  `WAIST US` size rejected.

```
cd backend
python -m pytest tests/ -q --ignore=tests/test_database.py    # 287 passed
python -m pytest tests/test_matching.py -q                    # 74 passed
```

*(Historical `1.4.0` counts. After the `1.4.1` guard the suite is **299 passed**
and `test_matching.py` **86 passed** — see §8.)*

## 5. Live-DB verification (original engine 1.4.0 run — historical)

Running the **real** implemented engine over the 62,647-row catalogue
reproduces the simulated variant C exactly: **130 SSENSE clusters / 130 matched
SSENSE SKUs**, **155** new pairs involving SSENSE, **6** new pure-non-SSENSE
pairs (all the same real product — see §6).

| Metric | before (1.3.1) | after (1.4.0) | Δ |
|---|---:|---:|---:|
| `total_skus` | 62,647 | 62,647 | 0 |
| `matched_skus` | 7,180 | **7,405** | +225 |
| `coverage_pct` | 11.461 | **11.8202** | +0.3592 |
| `back_in_stock_opportunities` | 317 | **364** | +47 |
| `back_in_stock_skus` | 405 | **449** | +44 |
| `matched_groups` | 3,480 | **3,586** | +106 |
| `ssense` matched / coverage / opps | 0 / 0.0 / 0 | **130 / 4.4384 / 61** | — |

Confirmed genuine matches include:

- `ssense:Blue Simple Pant Jeans/Blue/2832` ↔ `carhartt_wip:Simple Pant/Blue/2832`
  (after `WAIST US 28 x 32` -> `2832`);
- `ssense:Black OG Active Jacket/Black` ↔ `carhartt_wip:OG Active Jacket/Black`;
- `ssense:White V-90 Leather Sneakers/White/40` ↔ `veja:V-90 LEATHER WHITE BLACK/White / Black/40`
  (generic-tail `Sneakers` trim; `IT 40` -> `40`).

## 6. Known residual effect (accepted)

Six new pairs connect two **non**-SSENSE shops, all the same product —
`carhartt_wip`/`asphaltgold`/`xtreme`/`afew` **OG Detroit Jacket** (Black Stone
Canvas), where **afew omits the colour** entirely. These are *correct* matches
enabled by the pre-existing `_split_by_colour` rule ("a single resolved colourway
absorbs colourless members") once the SSENSE bridge forms the cluster. They are
not false positives and were accepted rather than special-casing
`_split_by_colour` (which would risk the validated engine).

## 7. Limitations

- Matching is still **exact** on the base identity: SSENSE products whose titles
  do not reduce to a matching base key (or that lack a compatible colour on the
  other side) remain unmatched. No fuzzy/approximate matching was added.
- The `WAIST US`/`IT` prefixes are stripped brand-agnostically, but the patterns
  only ever fire for SSENSE-shaped sizes, so other shops are unaffected.

## 8. Engine 1.4.1 — Prescott false-positive correction (current)

A post-implementation precision audit of the `1.4.0` engine found one confirmed
false positive.

**The false positive.** SSENSE `Green Prescott Coat` (structured colour
`Oxide green/Black`) matched Carhartt WIP `Prescott Coat` (`Black / Black`)
across **six sizes** (XS–XXL). Both titles reduce to the same `base_key`
(`prescott coat`), and `1.4.0`'s subset colour rule accepted the shared *accent*
word `black` even though the SSENSE headline colour is **green**. Two of the six
(M, XXL) were mixed-stock, so they also inflated `back_in_stock_opportunities` /
`back_in_stock_skus`. A global scan of the **130** colour-prefixed cross-shop
clusters confirmed these six were the **only** prefix-mismatch clusters.

**The fix (narrow — colour-prefixed side only).** The `base_key` path in
`_same_product` now additionally requires that the prefixed side's **leading
colour run** — the contiguous leading title words that are also in that SKU's own
colour (`normalization.leading_colour_run`, new) — is corroborated by the *other*
side's structured colour (`_prefixed_colour_corroborated`). It is a token-set
intersection (never a substring), so `Green Prescott Coat` (run `{green}`) is
rejected against `Black / Black` (`{green} ∩ {black} = ∅`), while genuine
multi-word variations are kept (SSENSE `Dusty Hamilton Brown` ↔ `Hamilton Brown`,
run `{hamilton, brown}`). No other `_same_product` path, threshold, or colour
rule changed; a colour-prefixed side that fails corroboration is simply rejected.

**Limitation (documented, not hidden).** The guard compares the leading *run*
rather than a single first token, so a match is still trusted when a *later*
colour word (not the first) is the shared one — bounded by the existing
`colour_compatible` subset check. Precision is therefore high but **not perfect**,
and the metrics are best-effort rather than ground truth.

**Metrics after the correction (engine `1.4.1`, live DB, read-only).** The change
removes exactly the six Prescott clusters and nothing else; the six pre-existing
pure-non-SSENSE OG Detroit pairs (§6) are preserved unchanged.

| Metric | 1.4.0 (historical, §5) | **1.4.1 (current)** |
|---|---:|---:|
| `total_skus` | 62,647 | 62,647 |
| `matched_skus` | 7,405 | **7,393** |
| `coverage_pct` | 11.8202 | **11.8010** |
| `matched_groups` | 3,586 | **3,580** |
| `back_in_stock_opportunities` | 364 | **362** |
| `back_in_stock_skus` | 449 | **447** |
| new SSENSE pairs | 155 | **149** |
| `ssense` matched / coverage / opps | 130 / 4.4384 / 61 | **124 / 4.2335 / 59** |

**Tests.** `backend/tests/test_matching.py` grew from 74 to **86** (12 new
regression tests: Prescott rejection, leading colour on the other side still
matching, reversed order, subset variation with a shared leading colour still
matching, `WAIST US`-only vs `W…` size distinctness, inseam distinctness,
generic-tail `base_key` safety, distinct-model non-merge, bridged-cluster
membership, verified-SSENSE-shape retention). Full permitted suite: **299 passed**.

## 9. Files

- `backend/app/matching/normalization.py`, `backend/app/matching/matcher.py` —
  the change (engine `1.4.0`), plus the `1.4.1` guard (`leading_colour_run`,
  `_prefixed_colour_corroborated`).
- `backend/tests/test_matching.py` — 13 new synthetic regression tests for
  `1.4.0`, and 12 more for the `1.4.1` guard (74 → 86).
- `README.md` — SSENSE matching note, Known limitations, algorithm version.
- `chats/16_ssense_scraper.md` — matching paragraph updated.
