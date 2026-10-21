"""Cross-shop SKU matching + dashboard metrics.

Overview
--------
A **SKU** is one product + one size + one shop. Two SKUs are considered the
*same product* when, across **different shops**, they agree on:

1. **Brand** — required and non-empty.
2. **Size** — required and non-empty (normalized; see ``normalization``).
3. **Product identity** — either the ``normalized(title + colour)`` composite is
   equal (so a shop that folds colour into the title matches one that keeps the
   colour in the colour field), *or* the normalized titles are equal **and both
   sides carry a colour** that is compatible, *or* the titles agree once their
   own colour words are removed (``base_key``) and at least one side begins with
   its colour (SSENSE-style ``"Black OG Active Jacket"``); on that last path the
   prefixed side's **leading colour run must also appear in the other side's
   colour** (a shared *accent* word is not enough).
4. **Colour compatibility** — when both sides have a non-empty colour one word
   set must be a **subset** of the other. An empty colour is **not** a wildcard:
   it never provides evidence, so two title-equal SKUs only match when both
   colours are present and compatible.
5. **Style code** — when *both* sides carry a manufacturer style code (e.g.
   Carhartt WIP ``I037234_1``) they must be equal, else rejected.

Matching is only ever performed **across different shops**; SKUs within one
shop are never grouped. Groups are formed with union-find so that a product
that appears in several shops is one cluster, and each cluster is then split
into colourways so no empty colour field can bridge two distinct colours.

Headline metrics
----------------
- **total_skus** — every SKU record.
- **matched_skus** — SKUs belonging to a cluster that spans ≥2 shops.
- **coverage_pct** — ``matched_skus / total_skus * 100``.
- **back_in_stock_opportunities** — matched product+size clusters whose stock
  is *mixed* across shops (available in at least one shop while sold out in at
  least one other). Each is a candidate for restock / cross-shop redirection.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from app.matching.normalization import (
    base_product_key,
    build_colour_vocabulary,
    canonical_brand,
    colour_tokens,
    derive_colour_from_handle,
    extract_style_code,
    is_colour_prefixed,
    leading_colour_run,
    normalize_colour,
    normalize_size,
    normalize_title,
)

ALGORITHM_VERSION = "1.4.1"


# ---------------------------------------------------------------------------
# Normalized representation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NormalizedSKU:
    index: int
    shop: str
    brand_key: str
    title_key: str
    composite_key: str
    size_key: str
    colour: frozenset
    style_code: str
    in_stock: bool
    raw: Mapping[str, Any]
    derived_colour: str = ""
    base_key: str = ""
    colour_prefixed: bool = False


def normalize_sku(
    raw: Mapping[str, Any],
    index: int = 0,
    vocabulary: Iterable[str] | None = None,
) -> NormalizedSKU:
    """Normalize one raw SKU document.

    The scraped ``colour`` field always wins. Only when it is empty, and a
    colour vocabulary is supplied, do we *conservatively* derive a colour from
    the URL handle (see ``derive_colour_from_handle``). The derived value is
    kept in ``derived_colour`` for transparency and never overwrites ``raw``.

    ``composite_key`` uses the **raw** colour only, so a derived colour can never
    move a SKU out of an identity bucket it already belonged to (that is what
    keeps already-validated matches stable); the derived colour is used purely as
    colourway evidence by the matcher.
    """
    shop = str(raw.get("shop") or "").strip()
    brand_key = canonical_brand(raw.get("brand"))
    title_key = normalize_title(raw.get("title"))

    raw_colour = normalize_colour(raw.get("colour"))
    derived = ""
    if not raw_colour and vocabulary:
        derived = derive_colour_from_handle(
            raw.get("title"), raw.get("url"), vocabulary
        )
    effective_colour = raw_colour or derived
    colour = colour_tokens(effective_colour)

    return NormalizedSKU(
        index=index,
        shop=shop,
        brand_key=brand_key,
        title_key=title_key,
        composite_key=f"{title_key} {raw_colour}".strip(),
        size_key=normalize_size(raw.get("size"), brand_key),
        colour=colour,
        style_code=extract_style_code(raw.get("barcode")),
        in_stock=bool(raw.get("in_stock")),
        raw=raw,
        derived_colour=derived,
        base_key=base_product_key(title_key, colour),
        colour_prefixed=is_colour_prefixed(title_key, colour),
    )


def normalized_skus(
    records: Sequence[Mapping[str, Any]],
    vocabulary: Iterable[str] | None = None,
) -> List[NormalizedSKU]:
    return [normalize_sku(doc, i, vocabulary) for i, d in enumerate(records)]


# ---------------------------------------------------------------------------
# Pairwise match predicate
# ---------------------------------------------------------------------------

def colour_compatible(a: NormalizedSKU, b: NormalizedSKU) -> bool:
    """Colour compatibility for a pair where *both* sides carry a colour.

    For colours taken from a shop's own field this requires one word set to be a
    **subset** of the other, which accepts the common cross-shop pattern where
    one shop adds descriptive words (``"Black"`` vs ``"Black Rinsed"``,
    ``"Barchan"`` vs ``"Barchan Stone Dyed"``) while rejecting genuinely
    different colourways that merely share an accent word.

    When *either* side's colour was **derived from a URL handle** the test is
    stricter — the word sets must be **equal**. A handle often abbreviates the
    colourway (``...-landon-pant-blue252`` -> ``"blue"``), and a bare ``"blue"``
    would otherwise be a subset of every ``"Blue ..."`` colourway and bridge two
    distinct products (``"Blue Rinsed"`` and ``"Blue Marble Used Wash"``).
    Requiring equality keeps derived evidence safe.
    """
    if not a.colour or not b.colour:
        return False
    if a.derived_colour or b.derived_colour:
        return a.colour == b.colour
    return a.colour <= b.colour or b.colour <= a.colour


def _prefixed_colour_corroborated(
    prefixed: NormalizedSKU, other: NormalizedSKU
) -> bool:
    """Whether a colour-prefixed title's *headline* colour is on the other side.

    ``prefixed``'s title begins with one of its own colour words (see
    ``is_colour_prefixed``); the contiguous leading run of such words is the
    shop's headline colour (``normalization.leading_colour_run``). For the
    base-key path to be trustworthy that headline colour must be corroborated by
    ``other``'s **structured colour field** — it is not enough to share some
    *other* word the two colour strings happen to contain.

    Both operands are already normalized by ``normalization``: ``title_key`` is
    the lowercased, punctuation-collapsed title and ``colour`` is the
    ``colour_tokens`` word set, so this is a token-set intersection (the same
    vocabulary ``colour_compatible`` uses), never a substring match. It rejects
    SSENSE ``"Green Prescott Coat"`` (colour ``"Oxide green/Black"``) against
    Carhartt WIP ``"Prescott Coat"`` (colour ``"Black / Black"``): the headline
    ``green`` is not in the other side's colour, even though the accent ``black``
    is — the subset rule alone would wrongly accept it. It keeps genuine
    variations such as ``"Dusty Hamilton Brown"`` vs ``"Hamilton Brown"``, whose
    leading run shares ``hamilton brown``.

    Limitation: the guard compares the *leading colour run* rather than a single
    first token, because the title's first token can itself be a modifier of a
    longer colourway (the case above). It therefore still trusts a match when a
    later colour word (not the first) is the shared one — acceptable because the
    existing ``colour_compatible`` subset check already bounds what can match.
    """
    run = leading_colour_run(prefixed.title_key, prefixed.colour)
    return bool(run & other.colour)


def _same_product(a: NormalizedSKU, b: NormalizedSKU) -> bool:
    """Whether two SKUs are the same product, requiring colour evidence.

    A pair must carry *proof* that the colourway is the same — an empty colour
    is **not** a wildcard:

    * ``composite_key`` (``normalize(title + colour)``) equality means the
      colour is already encoded in the title, so a shop that folds colour into
      the title matches one that keeps it in the colour field;
    * otherwise, equal titles only match when **both** sides carry a colour and
      those colours are compatible;
    * finally, ``base_key`` equality (the title with its own colour words
      removed) bridges the case where one shop prefixes the colour into the
      title while the other keeps it separate — e.g. SSENSE ``"Black OG Active
      Jacket"`` (colour ``Black``) and DTC ``"OG Active Jacket"`` (colour
      ``Black``). This path is taken **only** when at least one side is actually
      colour-prefixed, so shops that never prefix their titles are unaffected,
      and it still requires compatible colours. In addition, every
      colour-prefixed side's leading colour run must be corroborated by the
      other side's structured colour (``_prefixed_colour_corroborated``), so a
      shared *accent* word cannot bridge two different colourways.

    This stops a colour-bearing SKU from bridging a colourless one and dragging
    different colourways (e.g. a DTC ``Black`` jacket and a retailer's
    unlabelled blue/camo jacket) into the same group.
    """
    if a.composite_key and a.composite_key == b.composite_key:
        return True
    if a.title_key and a.title_key == b.title_key:
        return colour_compatible(a, b)
    if (
        a.base_key
        and a.base_key == b.base_key
        and (a.colour_prefixed or b.colour_prefixed)
    ):
        if a.colour_prefixed and not _prefixed_colour_corroborated(a, b):
            return False
        if b.colour_prefixed and not _prefixed_colour_corroborated(b, a):
            return False
        return colour_compatible(a, b)
    return False


def _structural_match(a: NormalizedSKU, b: NormalizedSKU) -> bool:
    """Brand, size, cross-shop and style-code checks (colour handled separately)."""
    if a.shop == b.shop:
        return False
    if not a.brand_key or a.brand_key != b.brand_key:
        return False
    if not a.size_key or a.size_key != b.size_key:
        return False
    if a.style_code and b.style_code and a.style_code != b.style_code:
        return False
    return True


def skus_match(a: NormalizedSKU, b: NormalizedSKU) -> bool:
    """Whether two normalized SKUs are the same product+size in different shops."""
    return _structural_match(a, b) and _same_product(a, b)


# ---------------------------------------------------------------------------
# Clustering (union-find, blocked by brand+size)
# ---------------------------------------------------------------------------

@dataclass
class Cluster:
    key: Tuple[str, str]  # (brand_key, size_key)
    members: List[NormalizedSKU]

    @property
    def shops(self) -> set:
        return {m.shop for m in self.members}

    @property
    def is_cross_shop(self) -> bool:
        return len(self.shops) > 1

    @property
    def has_mixed_stock(self) -> bool:
        return any(m.in_stock for m in self.members) and any(
            not m.in_stock for m in self.members
        )


def _find(parent: List[int], x: int) -> int:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union(parent: List[int], a: int, b: int) -> None:
    ra, rb = _find(parent, a), _find(parent, b)
    if ra != rb:
        parent[ra] = rb


def _clique_partition(coloured: List[NormalizedSKU]) -> List[List[NormalizedSKU]]:
    """Greedily group colour-bearing members into *pairwise* compatible cliques.

    The subset relation is not transitive (e.g. ``Black`` and ``Stone Canvas``
    are both subsets of ``Black Stone Canvas``), so connected components could
    merge two genuinely different colourways; requiring a clique prevents that.
    """
    groups: List[List[NormalizedSKU]] = []
    for sku in sorted(coloured, key=lambda m: len(m.colour)):
        for group in groups:
            if all(colour_compatible(sku, other) for other in group):
                group.append(sku)
                break
        else:
            groups.append([sku])
    return groups


def _split_by_colour(members: List[NormalizedSKU]) -> List[List[NormalizedSKU]]:
    """Split an identity group into colourways.

    Members that carry a colour are partitioned into groups that are *pairwise*
    compatible (a clique, because the subset relation is not transitive). Any
    colourless member reached this point matched by composite — its title
    already contains the colour — so if the group resolved to a single
    colourway it joins it; if several colourways remain it is not attached to a
    specific one. The ``len(groups) == 1`` merge keeps the composite case
    together; the multi-group case never lets a colourless member bridge two
    distinct colourways.
    """
    coloured = [m for m in members if m.colour]
    empties = [m for m in members if not m.colour]

    # Common case: every colour was scraped. Behaviour is unchanged from the
    # validated engine, so clusters without derived colours are byte-identical.
    if not any(m.derived_colour for m in members):
        groups = _clique_partition(coloured)
        if not groups:
            return [empties] if empties else []
        if len(groups) == 1:
            return [groups[0] + empties]
        if empties:
            groups.append(empties)
        return groups

    # Some colourway evidence was derived from a URL handle. Plain (scraped)
    # members define the colourways first; derived members are then attached
    # only when unambiguous, so a bare derived colour can neither bridge two
    # colourways nor orphan a real one.
    plain = [m for m in coloured if not m.derived_colour]
    derived = [m for m in coloured if m.derived_colour]
    groups = _clique_partition(plain)

    unresolved: List[NormalizedSKU] = []
    for sku in sorted(derived, key=lambda m: len(m.colour)):
        exact = [g for g in groups if any(sku.colour == x.colour for x in g)]
        if len(exact) == 1:
            exact[0].append(sku)
            continue
        if not exact:
            compatible = [
                g for g in groups if all(colour_compatible(sku, x) for x in g)
            ]
            if len(compatible) == 1:
                compatible[0].append(sku)
                continue
        unresolved.append(sku)

    # Derived members left without a unique home may still pair with each other
    # when their derived colour is *identical* (e.g. the same handle colour on
    # two shops with no scraped-label counterpart).
    for sku in unresolved:
        for group in groups:
            if group and all(
                x.derived_colour and x.colour == sku.colour for x in group
            ):
                group.append(sku)
                break
        else:
            groups.append([sku])

    # Attach colourless members to the colourway whose composite key they equal;
    # this recovers the original composite match without letting them bridge two
    # remaining colourways.
    loose: List[NormalizedSKU] = []
    for empty in empties:
        targets = [
            g
            for g in groups
            if any(
                empty.title_key == x.composite_key for x in g if x.colour
            )
        ]
        if len(targets) == 1:
            targets[0].append(empty)
        else:
            loose.append(empty)
    if len(groups) == 1:
        groups[0].extend(loose)
    elif loose:
        groups.append(loose)
    return groups


def _is_ambiguous(members: List[NormalizedSKU]) -> bool:
    """Whether a group contains two indistinguishable SKUs from one shop.

    Two members of the same shop with the same title/composite key **and** the
    same colour are interchangeable: we cannot tell which one is the true
    counterpart of a cross-shop match (typically duplicate listings, or several
    unlabelled colourways of one model). Matching them would invent stock
    signals, so such groups are dropped rather than guessed at.
    """
    seen = set()
    for m in members:
        key = (m.shop, m.title_key, m.composite_key, m.colour)
        if key in seen:
            return True
        seen.add(key)
    return False


def build_clusters(skus: Sequence[NormalizedSKU]) -> List[Cluster]:
    """Group matching SKUs across shops.

    Blocking key is ``(brand, size)`` so only plausible pairs are compared;
    within a block, members are grouped by ``title_key`` and by
    ``composite_key``. Identity *and* colour must agree (an empty colour is not
    a wildcard), then each identity group is split into colourways and any
    group that is ambiguous within a shop is discarded.
    """
    parent = list(range(len(skus)))

    blocks: Dict[Tuple[str, str], List[NormalizedSKU]] = defaultdict(list)
    for sku in skus:
        if not sku.brand_key or not sku.size_key:
            continue
        blocks[(sku.brand_key, sku.size_key)].append(sku)

    for group in blocks.values():
        if len(group) < 2:
            continue
        by_key: Dict[str, List[NormalizedSKU]] = defaultdict(list)
        for sku in group:
            by_key[sku.title_key].append(sku)
            if sku.composite_key != sku.title_key:
                by_key[sku.composite_key].append(sku)
            if (
                sku.base_key
                and sku.base_key != sku.title_key
                and sku.base_key != sku.composite_key
            ):
                by_key[sku.base_key].append(sku)

        for members in by_key.values():
            if len(members) < 2:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    if skus_match(a, b):
                        _union(parent, a.index, b.index)

    grouped: Dict[int, List[NormalizedSKU]] = defaultdict(list)
    for sku in skus:
        grouped[_find(parent, sku.index)].append(sku)

    clusters: List[Cluster] = []
    for members in grouped.values():
        for colour_group in _split_by_colour(members):
            if len(colour_group) < 2 or len({m.shop for m in colour_group}) < 2:
                continue
            if _is_ambiguous(colour_group):
                continue
            clusters.append(
                Cluster(
                    key=(colour_group[0].brand_key, colour_group[0].size_key),
                    members=colour_group,
                )
            )
    return clusters


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class Metrics:
    total_skus: int = 0
    matched_skus: int = 0
    coverage_pct: float = 0.0
    back_in_stock_opportunities: int = 0
    back_in_stock_skus: int = 0
    matched_groups: int = 0
    shops: List[str] = field(default_factory=list)
    by_shop: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    by_brand: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    algorithm_version: str = ALGORITHM_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_skus": self.total_skus,
            "matched_skus": self.matched_skus,
            "coverage_pct": self.coverage_pct,
            "back_in_stock_opportunities": self.back_in_stock_opportunities,
            "back_in_stock_skus": self.back_in_stock_skus,
            "matched_groups": self.matched_groups,
            "shops": self.shops,
            "by_shop": self.by_shop,
            "by_brand": self.by_brand,
            "algorithm_version": self.algorithm_version,
        }


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator * 100.0, 4)


def compute_metrics(
    skus: Sequence[NormalizedSKU],
    clusters: Optional[Sequence[Cluster]] = None,
) -> Metrics:
    """Compute the dashboard metrics for a set of normalized SKUs.

    Matching rules are unchanged. ``clusters`` is an optional prebuilt result so
    a caller that also needs the groups (the dashboard API) can run the
    union-find once instead of once per endpoint; omitted, it is built here.
    """
    total = len(skus)
    metrics = Metrics(total_skus=total, shops=sorted({s.shop for s in skus}))

    # per-shop / per-brand totals
    shop_totals: Dict[str, int] = defaultdict(int)
    shop_in_stock: Dict[str, int] = defaultdict(int)
    brand_totals: Dict[str, int] = defaultdict(int)
    for s in skus:
        shop_totals[s.shop] += 1
        if s.in_stock:
            shop_in_stock[s.shop] += 1
        brand_totals[s.brand_key] += 1

    shop_matched: Dict[str, int] = defaultdict(int)
    brand_matched: Dict[str, int] = defaultdict(int)
    shop_opp: Dict[str, int] = defaultdict(int)
    brand_opp: Dict[str, int] = defaultdict(int)

    clusters = build_clusters(skus) if clusters is None else clusters
    for cluster in clusters:
        if not cluster.is_cross_shop:
            continue
        for m in cluster.members:
            shop_matched[m.shop] += 1
            brand_matched[m.brand_key] += 1
        if cluster.has_mixed_stock:
            metrics.back_in_stock_opportunities += 1
            metrics.back_in_stock_skus += sum(
                1 for m in cluster.members if not m.in_stock
            )
            for shop in cluster.shops:
                shop_opp[shop] += 1
            brand_opp[cluster.members[0].brand_key] += 1

    metrics.matched_skus = sum(shop_matched.values())
    metrics.coverage_pct = _pct(metrics.matched_skus, total)
    metrics.matched_groups = sum(1 for c in clusters if c.is_cross_shop)

    metrics.by_shop = {
        shop: {
            "total_skus": shop_totals[shop],
            "matched_skus": shop_matched[shop],
            "coverage_pct": _pct(shop_matched[shop], shop_totals[shop]),
            "in_stock": shop_in_stock[shop],
            "out_of_stock": shop_totals[shop] - shop_in_stock[shop],
            "back_in_stock_opportunities": shop_opp[shop],
        }
        for shop in sorted(shop_totals)
    }
    metrics.by_brand = {
        brand: {
            "total_skus": brand_totals[brand],
            "matched_skus": brand_matched[brand],
            "coverage_pct": _pct(brand_matched[brand], brand_totals[brand]),
            "back_in_stock_opportunities": brand_opp[brand],
        }
        for brand in sorted(brand_totals)
    }
    return metrics


def matched_group_samples(
    clusters: Sequence[Cluster],
    limit: Optional[int] = 50,
    mixed_only: bool = False,
) -> List[Dict[str, Any]]:
    """Human-readable samples of cross-shop matched groups (for the API/UI).

    When ``mixed_only`` is set, only groups that are actually *back-in-stock
    opportunities* (mixed stock across shops) are emitted, so a caller that caps
    the number of groups can never drop a real opportunity in favour of an
    uninteresting same-stock group. ``limit`` of ``None`` means "no cap".
    """
    out: List[Dict[str, Any]] = []
    for cluster in clusters:
        if not cluster.is_cross_shop:
            continue
        if mixed_only and not cluster.has_mixed_stock:
            continue
        members = [
            {
                "shop": m.shop,
                "title": m.raw.get("title"),
                "colour": m.raw.get("colour"),
                "derived_colour": m.derived_colour,
                "size": m.raw.get("size"),
                "price": m.raw.get("price"),
                "currency": m.raw.get("currency"),
                "in_stock": m.in_stock,
                "url": m.raw.get("url"),
            }
            for m in cluster.members
        ]
        out.append(
            {
                "brand": cluster.key[0],
                "size": cluster.key[1],
                "shops": sorted(cluster.shops),
                "has_mixed_stock": cluster.has_mixed_stock,
                "members": members,
            }
        )
        if limit is not None and len(out) >= limit:
            break
    return out
