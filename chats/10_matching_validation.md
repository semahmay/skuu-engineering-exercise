# Pre-deploy validation of matching + back-in-stock

Read-only audit of the cross-shop matcher over the live 50,302-SKU dataset
before deployment. Goal: confirm the matched groups and the back-in-stock
opportunities are real, and remove false positives.

## Method

`backend/debug_validate.py` (gitignored) loads the collection through the
production code path (`normalize_sku` → `build_clusters` → `compute_metrics`)
and reports:

- overall counts, cross-shop cluster shop-pairs, brand split;
- heuristic false-positive scans (intra-cluster style-code/colour conflicts,
  a shop contributing several distinct products under one key);
- random samples of matched groups and of back-in-stock groups;
- reason classification of every cross-shop pair edge (title vs composite).

## Findings

### Bug 1 — empty-colour wildcard bridged colourways

`colour_compatible` returned `True` whenever either side had no colour, and
union-find then merged identity groups that were only linked through that
wildcard. Concretely a DTC `OG Active Jacket` (`colour="Black"`) matched
unlabelled retailer `OG Active Jacket` rows for **blue / camo** jackets and
pulled them into one cluster. 402 cross-shop pair edges depended on the
wildcard.

**Fix** (`matcher.py`): matching now requires *colour evidence*.
`_same_product` matches when either

- the `normalize(title + colour)` composite is equal (colour folded into the
  title), or
- titles are equal **and both sides carry a compatible colour**.

An empty colour is no longer a wildcard. `skus_match = _structural_match and
_same_product`.

### Bug 2 — ambiguous groups (multiple products under one key)

Even after Bug 1 there were 432 clusters where one shop contributed several
indistinguishable SKUs under the same key (e.g. afew listing 3 unlabelled
colourways of one model; caliroots/footway duplicate listings). 2,724 matched
SKUs and **248 of 490 opportunities (~51%)** were affected.

**Fix** (`matcher.py`): `_is_ambiguous` drops any group containing two members
from the same shop with the same `(title_key, composite_key, colour)` — we cannot
tell which is the true counterpart, so the group is excluded rather than guessed.

### Non-issues confirmed

- 0 intra-cluster style-code conflicts, 0 incompatible colour pairs.
- Random samples of opportunities were all genuine (colour either folded into
  the title on both sides, or a subset match such as DTC `Blue` vs AG `Blue
  Stone Washed`).

## Result

| Metric | Before validation | After validation |
| --- | ---: | ---: |
| `matched_skus` | 10,230 | 6,812 |
| `coverage_pct` | 20.3372 | 13.5422 |
| `back_in_stock_opportunities` | 601 | 242 |
| `back_in_stock_skus` | 1,588 | 323 |
| `matched_groups` | 3,980 | 3,316 |
| `total_skus` | 50,302 | 50,302 |

`algorithm_version` → `1.1.0`. Coverage dropped because previously-counted false
matches were removed (conservative policy: prefer a miss over a false match).

## Tests added (`backend/tests/test_matching.py`, now 28 tests)

- `test_empty_colour_is_not_a_wildcard` — title-equal, one colour → no match.
- `test_composite_match_with_empty_colour_field` — colour-in-title vs
  colour-in-field still matches.
- `test_empty_colour_does_not_match_colourful_by_title`.
- `test_ambiguous_group_excluded` and `test_unambiguous_empty_colour_still_matches`.
- `test_empty_colour_does_not_bridge_colourways`.
- `test_non_transitive_subset_does_not_merge_colourways` (existing).

Full suite: **165 passed**.

## Residual limitations

- Retailers that publish no colour (afew, sneakersnstuff, biffi) contribute
  few/no matches because their colourways are indistinguishable; extracting
  colour from the product handle/URL would recover them. *(Addressed in
  `11_colour_extraction.md`: conservative handle extraction recovered 170
  matched SKUs / 124 cross-shop pairs with **zero** regressions.)*
- The colour **subset** rule still treats `Black` and `Black Rinse` as the same
  colourway by design (4 residual Blevin clusters, all in-stock, no false
  opportunities).
- Empty-colour opportunities where *both* shops omit colour (62 of 242) remain a
  best-effort signal.
- Salomon DTC matches nothing (UK vs EU sizes; no conversion by design).

## Verification commands

```bash
cd backend
python debug_validate.py          # audit report
python -m pytest tests/ -q        # 165 passed
python debug_api_smoke.py         # live ASGI: /metrics + /matching/groups
```
