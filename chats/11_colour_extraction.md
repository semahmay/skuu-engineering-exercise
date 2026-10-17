# Handle-derived colourways (matching engine v1.2.0)

Fourth workstream: recover legitimate cross-shop matches that the validated
`1.1.0` engine was missing, **without** reintroducing any false positive. The
missing matches came from shops that publish no colour field (their colourway is
only visible in the product URL handle), so a shop listing several colourways of
one model looked "ambiguous" and was dropped.

> Constraint honoured throughout: **never weaken ambiguity detection, never bring
> back the empty-colour wildcard, never alter scraped data, prefer a miss over a
> false match.** Handle colour is a last resort and is accepted only when it is
> provably safe.

## 1. Problem

The `1.1.0` engine requires *colour evidence* to match two title-equal SKUs. A
shop that only exposes `Size` (afew, sneakersnstuff, nugnes1920, …) contributes
no colour, so:

- afew's Carhartt WIP SKUs matched **0** times even though the colourway is in
  the URL (`.../carhartt-wip-arizona-jacket-tobacco`);
- where such a shop listed several unlabelled colourways of one model, the group
  was (correctly, then) treated as ambiguous and excluded.

## 2. Handle inspection (what is actually recoverable)

`backend/debug_inspect_handles.py` (gitignored) dumped the empty-colour URLs per
shop before writing any extraction code:

| Shop | Handle looks like | Colour in handle? | Recoverable |
| --- | --- | --- | --- |
| **afew** | `carhartt-wip-arizona-jacket-tobacco` | Yes — title then a canonical colour | **Yes** (largest gain) |
| **nugnes1920** | `.../products/veja-v-10-white-natural` | Sometimes | Some |
| **biffi** | `...-detroit-jacket-i035817-895f` | Mostly Carhartt colour **codes** (`895f`, `3tfxx`) | A few explicit words only |
| **caliroots** / **footway** | colour already in the **title**, handle mirrors it | Rarely extra | A few unlabelled handles |
| **beamhill** | title already spells the colour, handle repeats it | Redundant | No (guard rejects) |
| **sneakersnstuff** | `.../product-49675` (style code) | No | **No** — left unmatched |

`build_colour_vocabulary` is built from the colours shops actually publish, so a
handle word is only ever accepted if it is a real colourword from the dataset.

## 3. Extraction rules (`normalization.py`)

`build_colour_vocabulary(records)` — union of every non-empty `colour` field,
tokenized; keeps tokens with `len >= 2`, not all digits, and not in
`COLOUR_VOCABULARY_STOPWORDS = {"no","not","color","colour","listed","n"}`.

`derive_colour_from_handle(title, url, vocabulary)`:

1. Take the **last path segment** of the URL (drop query/fragment).
2. Locate the normalized title inside the handle as an **in-order token
   subsequence** (brand tokens already stripped). If the title is not present →
   `""`.
3. The **trailing** tokens are the colour candidate. Drop pure digits (`-2`,
   `-3`), manufacturer style codes (`^[a-z]{1,3}\d{3,}…`, `^\d{3,}[a-z]{0,3}$`,
   e.g. `i032940`, `890r`), and strip trailing digits (`washed1` → `washed`).
4. **Accept only if every remaining word is in the vocabulary.** Any unknown
   word → `""` (ambiguous).
5. If the candidate words are already in the title, return `""` — the handle
   merely repeats a colour the title already carries (redundancy guard; without
   it, beamhill's title-based matches would break).

A missing colour is never inferred and never becomes a wildcard.

## 4. Matching integration (`matcher.py`)

- `normalize_sku(raw, index, vocabulary)` uses the derived colour **only when the
  scraped `colour` field is empty**. The scraped field always wins. The derived
  value is stored in `NormalizedSKU.derived_colour` and **never overwrites**
  `raw`.
- **`composite_key` stays based on the scraped colour only.** This is the key to
  zero regression: any identity group that contains no derived member is
  byte-identical to `1.1.0`.
- `colour_compatible`: if *either* side's colour was derived, the word sets must
  be **equal** (not subset). A bare derived `blue` would otherwise be a subset of
  every `Blue …` colourway and bridge `Blue Rinsed` ↔ `Blue Marble Used Wash`.
- `_split_by_colour`: when a group contains derived members, the *plain* (scraped)
  members define the colourways first (pairwise-compatible clique). Each derived
  member is then attached only if it is compatible with **exactly one** group
  (preferring a unique exact-colour group); ambiguous derived members are dropped
  or grouped only with identical derived colours. Colourless members attach to
  the colourway whose `composite_key` they equal. This stops a derived colour from
  either **bridging** two colourways or **orphaning** a real one.

### Rejected alternative

Using the *effective* (raw-or-derived) colour in `composite_key` was tried first.
It recovered more pairs but **lost 20 validated pairs** — e.g. caliroots/footway
`Simple Pant` (unlabelled, derived `blue`) got merged into the same bucket as the
duplicate `Simple Pant Blue` listings, whose ambiguity then dropped the whole
group; and asphaltgold's `Parrish Pant Obsidian Stone Washed` was orphaned when
the derived `obsidian` clique-split it away from `Carhartt WIP`. Keeping the
composite on scraped data and handling derived members only in colour logic
removes both regressions.

## 5. Result (v1.1.0 → v1.2.0)

| Metric | `1.1.0` | `1.2.0` | Δ |
| --- | ---: | ---: | ---: |
| `total_skus` | 50,302 | 50,302 | 0 |
| `matched_skus` | 6,812 | **6,982** | +170 |
| `coverage_pct` | 13.5422 | **13.8802** | +0.338 |
| `back_in_stock_opportunities` | 242 | **285** | +43 |
| `back_in_stock_skus` | 323 | **373** | +50 |
| `matched_groups` | 3,316 | **3,388** | +72 |

Pair-level diff (`debug_colour_compare.py`): cross-shop pairs `3714 → 3838`,
**lost = 0**, **gained = 124**; matched SKU ids **lost = 0**.

Gained pairs by shop-pair: afew↔asphaltgold 62, afew↔carhartt_wip 45,
afew↔beamhill 11, afew↔xtreme 6.

Per-shop matched: afew `0 → 98`, asphaltgold `300 → 340`, carhartt_wip
`304 → 336`; every other shop unchanged. All sampled new clusters are genuine
(title + identical colour, e.g. `Hooded Nelson Jacket` black in
carhartt_wip + afew + beamhill).

Derived colours: **2,410** SKUs (afew 2,131, biffi 97, nugnes1920 80, caliroots
41, footway 45, beamhill 16). sneakersnstuff derives none (style-code handles) and
stays unmatched by design.

## 5.1 Why lost = 0

Because `composite_key` ignores derived colour, every cluster that validates
without a derived member is unchanged. Clusters that *gain* an afew/retailer
member are handled by the derived-aware colour split, which never removes the
previously validated plain members and never attaches a bare derived colour to a
colourway it cannot prove.

## 6. Tests

`backend/tests/test_matching.py` grew by 22 tests (**full suite 187 passing**):

- `TestHandleColourExtraction` — genuine trailing colour, disambiguation digit,
  multi-word colour, unknown word → `""`, style code → `""`, numeric colour code
  → `""`, title absent → `""`, colour already in title → `""`, empty vocabulary,
  empty URL; vocabulary drops placeholders/short tokens.
- `TestNormalizeWithVocabulary` — scraped colour wins; derived used only when
  empty; `composite_key` ignores derived colour.
- `TestDerivedColourCompatibility` — derived-equals-plain accepted; derived does
  not subset a plain superset; the plain subset rule is unchanged.
- `TestDerivedColourClustering` — derived bridges two shops; a bare derived colour
  does not pick between sibling colourways; adding a derived member does not
  orphan a plain colourway; redundant handle colour keeps the title match;
  identical derived colours match across shops.

## 7. Limitations

- Handles that end in a **style code** (sneakersnstuff) or a manufacturer
  **colour code** (biffi `895f`) yield nothing and stay unmatched — honest
  misses.
- Derived evidence is **equality-only** and strict: a bare `blue` will not match
  `Blue Rinsed`. That is deliberate (precision over recall).
- If a derived colour is compatible with more than one sibling colourway it is
  dropped rather than guessed.
- Handle extraction never changes scraped data; it is recomputed read-only at
  matching time.

## 7.1 Verification commands

```bash
cd backend
python debug_colour_compare.py     # v1.1.0 -> v1.2.0; lost=0, gained=124
python debug_gained_pairs.py       # spot-check every new cluster
python -m pytest tests/ -q         # 187 passed
python debug_api_smoke.py          # live ASGI: /metrics + /matching/groups (200)
```

Frontend: `npm run lint` clean; `npm run build` succeeds.

## 8. Key files

- `backend/app/matching/normalization.py` — `build_colour_vocabulary`,
  `derive_colour_from_handle`, style-code/digit regexes, stopwords.
- `backend/app/matching/matcher.py` — `NormalizedSKU.derived_colour`,
  `normalize_sku(..., vocabulary)`, derived-equality `colour_compatible`,
  derived-aware `_split_by_colour`, `ALGORITHM_VERSION = "1.2.0"`.
- `backend/app/matching/service.py` — builds the vocabulary from the collection
  then normalizes (`load_normalized_skus`).
- `backend/tests/test_matching.py` — 22 new extraction/regression tests.
- `backend/debug_colour_compare.py`, `debug_gained_pairs.py`,
  `debug_inspect_handles.py` — local verification scripts (gitignored).

## 9. Follow-up (engine v1.3.1): one-size normalization + style-code capture

Two later, separate changes on top of `1.2.0`; both are conservative (precision
over recall). The matcher's identity rules were **not** weakened.

### 9.1 Salomon UK→EU mapping (`1.3.0`, context)

The Salomon DTC store publishes **UK** footwear sizes while EU retailers publish
**EU** sizes, so Salomon matched nothing. A brand-scoped `_SALOMON_UK_TO_EU`
table inside `normalize_size(size, brand)` maps an explicitly-`UK` Salomon size
to its EU equivalent — and only for Salomon; every other brand/size is
byte-identical to before. This took Salomon `0 → 360` matched (documented in the
README "Matching engine" section).

### 9.2 One-size normalization (`1.3.1`)

**Problem.** Shops label the same accessory ("one size") differently, and
`normalize_size` missed the Italian and dotted forms — so genuinely identical
accessories never met across shops:

- Biffi (Italian): `TU`, `Taglia Unica`;
- Veja DTC: dotted `O.S` (previously collapsed to the bogus key `o.s`);
- afew: the full phrase `One Size Fits All`.

**Fix (`normalization.py`).** Added `tu`, `unica`, `tagliaunica`,
`onesizefitsall` to `_ONE_SIZE_COMPACT`, and additionally compare the
dot-stripped form (`compact.replace(".", "")`), so `O.S` / `T.U` land on the same
`ONE_SIZE` token as `One Size` / `OS` / `UNI` / `NS`. No other size behaviour
changed; a decimal (`7.5`) still keeps its dot.

**Regression test.**
`tests/test_matching.py::TestNormalization::test_normalize_size_one_size_synonyms`
covers the dotted forms, the Italian forms, the full phrase, and confirms a
decimal is still a decimal.

**Measured impact** (live dataset, 50,302 SKUs; `1.3.0` → `1.3.1`):

| Metric | `1.3.0` | `1.3.1` | Δ |
| --- | ---: | ---: | ---: |
| `matched_skus` | 7,780 | **7,812** | **+32** |
| `matched_groups` | 3,738 | **3,752** | +14 |
| `coverage_pct` | 15.4666 | **15.5302** | +0.0636 |

Per-shop matched deltas: afew `98 → 116` (+18), carhartt_wip `336 → 347` (+11),
asphaltgold `583 → 584` (+1), salomon `360 → 361` (+1), sneakersnstuff `1 → 2`
(+1). Every new cluster was inspected and is a genuine accessory match — e.g.
`Harlem Cap` (asphaltgold `UNI` / afew `One Size Fits All` / beamhill `One
Size`) and `Spoiled Salt and Pepper Shakers` (sneakersnstuff `OS` ↔ afew `One
Size Fits All`).

### 9.3 Style-code capture (capture + storage only)

biffi / sneakersnstuff / nugnes1920 expose the Carhartt manufacturer style code
in `variant.sku` (e.g. `I032940_89`) but publish **no colour**, and a style code
alone cannot separate colourways. Using it as *identity* was tested and
**rejected** — it produced confirmed false positives (distinct colourways sharing
a base code; Veja `i342168` "GT Nolyn FULL BLACK" ↔ xtreme "Amarante/Butter").
Reading style codes from URL handles as a *veto* was also rejected (it lost 214
matches, because DTC barcodes carry a suffix while handles do not).

As a safe first step the code is now **captured and stored only**:

- `models/sku.py` — new optional `style_code` field, documented as *not* used for
  matching.
- `scrapers/shopify_retailer.py` — `extract_variant_style_code()` stores
  `variant.sku` verbatim when it is **not** GTIN-shaped (a GTIN identifies a
  barcode, not a product style) and sets it on the SKU.
- The matcher is **unchanged**: `style_code` is never an identity, the colour
  requirement is untouched, and ambiguous products are still dropped.

**Tests** lock this down — capture from `variant.sku`; `None` for GTIN-shaped /
missing SKUs; a stored `style_code` is not identity; different colourways that
share a style code stay unmatched; identical style codes at different sizes stay
unmatched (`test_shopify_retailer.py`, `test_matching.py`).

**Limitation (documented, not fixed).** Without colour information the style code
cannot safely separate colourways, so biffi / sneakersnstuff / nugnes1920 / veja
remain largely unmatched (0.0–0.6%). Capture enables a future, *validated*
identity change but changes no metric today.

### 9.4 Tests / verification

`test_matching.py` 57 → **61**, `test_shopify_retailer.py` +4; the safe backend
suite (matching + 5 scraper files) is **212 passing**. Live metrics with the
capture-only change were re-verified **unchanged** (50,302 / 7,812 / 15.5302 /
465 opportunities / 3,752 groups). No re-scrape, no live-DB mutation.

