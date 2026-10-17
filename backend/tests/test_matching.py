"""Tests for the cross-shop matching engine and dashboard metrics.

Pure-unit tests: no database or network. They pin the conservative matching
rules (never match on empty keys; colour subset rule; cross-shop only) and the
metric definitions.
"""

from app.matching.matcher import (
    build_clusters,
    colour_compatible,
    compute_metrics,
    matched_group_samples,
    normalize_sku,
    skus_match,
)
from app.matching.normalization import (
    base_product_key,
    build_colour_vocabulary,
    canonical_brand,
    colour_tokens,
    derive_colour_from_handle,
    extract_style_code,
    is_colour_prefixed,
    normalize_size,
    normalize_title,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def sku(shop, brand="Carhartt WIP", title="", colour="", size="", barcode=None,
        in_stock=True, url=""):
    return {
        "shop": shop,
        "brand": brand,
        "title": title,
        "colour": colour,
        "size": size,
        "barcode": barcode,
        "in_stock": in_stock,
        "url": url,
    }


def norm(*records):
    return [normalize_sku(r, i) for i, r in enumerate(records)]


def vocab(*colours):
    return build_colour_vocabulary([{"colour": c} for c in colours])


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_canonical_brand(self):
        assert canonical_brand("Carhartt") == "carhartt wip"
        assert canonical_brand("Carhartt WIP") == "carhartt wip"
        assert canonical_brand("Veja") == "veja"
        assert canonical_brand("") == ""
        assert canonical_brand(None) == ""

    def test_normalize_title_strips_brand_and_punctuation(self):
        assert normalize_title("Carhartt WIP OG Detroit Jacket") == "og detroit jacket"
        assert normalize_title("VEJA x Asphaltgold V-90") == "x asphaltgold v 90"
        assert normalize_title("") == ""

    def test_normalize_size(self):
        assert normalize_size("10.5 UK") == "10.5uk"
        assert normalize_size("10.5 UK") == normalize_size("10.5  uk")
        assert normalize_size("One Size") == "ONE_SIZE"
        assert normalize_size("OS") == "ONE_SIZE"
        assert normalize_size("") == ""

    def test_normalize_size_one_size_synonyms(self):
        # Dotted forms must not slip through as a bogus "o.s" key: the Veja store
        # publishes ``O.S`` for accessories while DTC stores publish ``One Size``.
        assert normalize_size("O.S") == "ONE_SIZE"
        assert normalize_size("O.S.") == "ONE_SIZE"
        # Italian ``TU`` / ``Taglia Unica`` (Biffi accessories) mean one size.
        assert normalize_size("TU") == "ONE_SIZE"
        assert normalize_size("T.U") == "ONE_SIZE"
        assert normalize_size("Taglia Unica") == "ONE_SIZE"
        # A decimal size keeps its dot (not mistaken for a one-size synonym).
        assert normalize_size("7.5") == "7.5"

    def test_normalize_size_ssense_prefixes(self):
        # SSENSE labels trousers/shoes with an explicit system prefix that other
        # shops omit; the prefix is stripped so the numeric parts can meet.
        assert normalize_size("WAIST US 28 x 32") == "2832"
        assert normalize_size("WAIST US 28 X 32") == normalize_size("28/32")
        assert normalize_size("WAIST US 30") == "30"
        assert normalize_size("IT 40") == "40"
        assert normalize_size("IT 40.5") == "40.5"
        # Distinct sizes never collapse onto each other.
        assert normalize_size("WAIST US 28 x 30") != normalize_size("28/32")

    def test_base_product_key_strips_own_colour_only(self):
        assert base_product_key("black og active jacket", {"black"}) == "og active jacket"
        # A word that is not this SKU's colour is kept even if it looks coloury.
        assert base_product_key("blue og active jacket", {"black"}) == "blue og active jacket"
        # Trailing generic nouns are trimmed.
        assert base_product_key("white v 90 leather sneakers", {"white"}) == "v 90 leather"
        assert base_product_key("simple pant jeans", {"blue"}) == "simple pant"
        # Stripping everything leaves an empty key (never an identity).
        assert base_product_key("black", {"black"}) == ""

    def test_is_colour_prefixed(self):
        assert is_colour_prefixed("black og active jacket", {"black"})
        assert not is_colour_prefixed("og active jacket", {"black"})
        assert not is_colour_prefixed("v 90 leather white black", {"white", "black"})
        assert not is_colour_prefixed("", set())

    def test_salomon_uk_size_maps_to_eu(self):
        # Salomon AU/NZ store publishes UK sizes; EU retailers publish EU sizes.
        assert normalize_size("5.5 UK", "salomon") == "3823"
        assert normalize_size("5.5 UK", "Salomon") == normalize_size("38 2/3")
        assert normalize_size("8.5 UK", "salomon") == normalize_size("42 2/3")
        assert normalize_size("13 UK", "salomon") == normalize_size("48 2/3")

    def test_salomon_mapping_only_applies_to_explicit_uk_footwear(self):
        # No brand -> unchanged (byte-identical default behaviour).
        assert normalize_size("5.5 UK") == "5.5uk"
        # Other brands never get size-system conversion.
        assert normalize_size("5.5 UK", "veja") == "5.5uk"
        assert normalize_size("5.5 UK", "carhartt wip") == "5.5uk"
        # A bare/EU Salomon size is left untouched.
        assert normalize_size("42", "salomon") == "42"
        assert normalize_size("42 2/3", "salomon") == "4223"
        # UK-looking value outside the table is not guessed.
        assert normalize_size("20 UK", "salomon") == "20uk"

    def test_extract_style_code_ignores_barcodes(self):
        assert extract_style_code("i037234_1") == "I037234_1"
        assert extract_style_code("4066747167273") == ""  # EAN-13
        assert extract_style_code("") == ""
        assert extract_style_code(None) == ""

    def test_colour_tokens(self):
        assert colour_tokens("Black / Gold") == frozenset({"black", "gold"})
        assert colour_tokens("") == frozenset()


# ---------------------------------------------------------------------------
# Pair predicate
# ---------------------------------------------------------------------------

class TestPairPredicate:
    def test_matches_same_product_different_shop(self):
        a, b = norm(
            sku("dtc", title="Floyde Short", colour="Ristretto", size="M"),
            sku("retail", title="Carhartt WIP Floyde Short",
                colour="Ristretto Garment Dyed", size="M"),
        )
        assert skus_match(a, b)

    def test_never_matches_same_shop(self):
        a, b = norm(
            sku("dtc", title="Floyde Short", colour="Ristretto", size="M"),
            sku("dtc", title="Floyde Short", colour="Ristretto", size="M"),
        )
        assert not skus_match(a, b)

    def test_never_matches_empty_brand_or_size(self):
        a, b = norm(
            sku("dtc", brand="", title="Floyde Short", size="M"),
            sku("retail", brand="", title="Floyde Short", size="M"),
        )
        assert not skus_match(a, b)
        c, d = norm(
            sku("dtc", title="Floyde Short", size=""),
            sku("retail", title="Floyde Short", size=""),
        )
        assert not skus_match(c, d)

    def test_different_brand_rejected(self):
        a, b = norm(
            sku("dtc", brand="Salomon", title="Speedcross", size="42"),
            sku("retail", brand="Veja", title="Speedcross", size="42"),
        )
        assert not skus_match(a, b)

    def test_different_title_rejected(self):
        a, b = norm(
            sku("dtc", title="Floyde Short", size="M"),
            sku("retail", title="Brader Short", size="M"),
        )
        assert not skus_match(a, b)

    def test_composite_title_colour_rule(self):
        # DTC folds colour into the title; retailer separates it.
        a, b = norm(
            sku("veja_dtc", brand="Veja", title="ETNA SUEDE PIERRE ALMOND", size="41"),
            sku("retail", brand="Veja", title="Etna Suede",
                colour="Pierre Almond", size="41"),
        )
        assert skus_match(a, b)

    def test_colour_subset_compatible(self):
        assert colour_compatible(
            normalize_sku(sku("a", colour="Black", size="M")),
            normalize_sku(sku("b", colour="Black Rinsed", size="M")),
        )

    def test_colour_distinct_vetoes(self):
        a, b = norm(
            sku("a", title="Chase Socks", colour="Black / Gold", size="ONE_SIZE"),
            sku("b", title="Chase Socks", colour="White / Gold", size="ONE_SIZE"),
        )
        assert not skus_match(a, b)

    def test_empty_colour_is_not_a_wildcard(self):
        # Same title, but only one side has a colour and the composites differ:
        # there is no proof it is the same colourway, so it must NOT match.
        a, b = norm(
            sku("a", title="Madison Logo Cap", colour="", size="ONE_SIZE"),
            sku("b", title="Madison Logo Cap", colour="Black / White", size="ONE_SIZE"),
        )
        assert not skus_match(a, b)

    def test_composite_match_with_empty_colour_field(self):
        # Colour folded into the title on one side, kept in the field on the
        # other -> composite equality is the proof it is the same colourway.
        a, b = norm(
            sku("retail", title="Madison Logo Cap Black / White", colour="",
                size="ONE_SIZE"),
            sku("dtc", title="Madison Logo Cap", colour="Black / White",
                size="ONE_SIZE"),
        )
        assert skus_match(a, b)

    def test_style_code_conflict_vetoes(self):
        a, b = norm(
            sku("dtc", title="Detroit Jacket", colour="Black", size="M",
                barcode="I032940_89"),
            sku("retail", title="Detroit Jacket", colour="Black", size="M",
                barcode="I032940_00"),
        )
        assert not skus_match(a, b)

    def test_missing_style_codes_do_not_become_identity(self):
        # A missing style code is never a wildcard: with an empty colour on one
        # side there is no proof of the same colourway, so it must NOT match.
        a, b = norm(
            sku("dtc", title="Detroit Jacket", colour="Black", size="M"),
            sku("retail", title="Detroit Jacket", colour="", size="M"),
        )
        assert not skus_match(a, b)

    def test_same_style_code_different_colourways_not_matched(self):
        # A shared style code is not identity: different colourways can carry the
        # same base code, so the colour rule still applies.
        a, b = norm(
            sku("dtc", title="Detroit Jacket", colour="Hamilton Brown", size="M",
                barcode="I032940_89"),
            sku("retail", title="Detroit Jacket", colour="Black", size="M",
                barcode="I032940_89"),
        )
        assert not skus_match(a, b)
        assert [c for c in build_clusters([a, b]) if c.is_cross_shop] == []

    def test_same_style_code_different_sizes_not_matched(self):
        a, b = norm(
            sku("dtc", title="Detroit Jacket", colour="Black", size="M",
                barcode="I032940_89"),
            sku("retail", title="Detroit Jacket", colour="Black", size="L",
                barcode="I032940_89"),
        )
        assert not skus_match(a, b)
        assert [c for c in build_clusters([a, b]) if c.is_cross_shop] == []

    def test_captured_style_code_field_is_not_matching_identity(self):
        # Capture-only: a stored ``style_code`` field is never used as identity.
        # Two shops may even hold the same code, but with no colour on one side
        # there is no proof it is the same colourway -> unmatched (documented
        # limitation of the style-code investigation).
        a_doc = sku("dtc", title="Detroit Jacket", colour="Black", size="M")
        b_doc = sku("retail", title="Detroit Jacket", colour="", size="M")
        a_doc["style_code"] = "I032940_89"
        b_doc["style_code"] = "I032940_89"
        a, b = norm(a_doc, b_doc)
        assert not skus_match(a, b)


# ---------------------------------------------------------------------------
# Clustering + metrics
# ---------------------------------------------------------------------------

class TestClustering:
    def test_links_across_three_shops_transitively(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M"),
            sku("b", title="Chase Tee", colour="Black Rinsed", size="M"),
            sku("c", title="Chase Tee", colour="Black", size="M"),
        )
        clusters = build_clusters(s)
        cross = [c for c in clusters if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"a", "b", "c"}

    def test_separate_sizes_do_not_merge(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M"),
            sku("b", title="Chase Tee", colour="Black", size="L"),
        )
        assert build_clusters(s) == []

    def test_singletons_excluded(self):
        s = norm(sku("a", title="Solo", colour="Black", size="M"))
        assert build_clusters(s) == []

    def test_empty_colour_does_not_match_colourful_by_title(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M"),
            sku("b", title="Chase Tee", colour="", size="M"),
        )
        assert build_clusters(s) == []

    def test_ambiguous_group_excluded(self):
        # A shop lists two indistinguishable unlabelled products under one key
        # (e.g. two colourways): the group cannot be matched safely.
        s = norm(
            sku("a", title="Chase Tee", colour="", size="M"),
            sku("a", title="Chase Tee", colour="", size="M"),
            sku("b", title="Chase Tee", colour="", size="M"),
        )
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert cross == []

    def test_unambiguous_empty_colour_still_matches(self):
        s = norm(
            sku("a", title="Chase Tee", colour="", size="M"),
            sku("b", title="Chase Tee", colour="", size="M"),
        )
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"a", "b"}

    def test_empty_colour_does_not_bridge_colourways(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M"),
            sku("b", title="Chase Tee", colour="Blue", size="M"),
            sku("c", title="Chase Tee", colour="", size="M"),
        )
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert cross == []

    def test_non_transitive_subset_does_not_merge_colourways(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M"),
            sku("b", title="Chase Tee", colour="Stone Canvas", size="M"),
            sku("c", title="Chase Tee", colour="Black Stone Canvas", size="M"),
        )
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        for c in cross:
            members = c.members
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    assert colour_compatible(members[i], members[j])


class TestMetrics:
    def test_counts_and_coverage(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M", in_stock=True),
            sku("b", title="Chase Tee", colour="Black", size="M", in_stock=False),
            sku("a", title="Solo Cap", colour="Red", size="ONE_SIZE"),
            sku("b", title="Solo Cap", colour="Blue", size="ONE_SIZE"),
        )
        m = compute_metrics(s)
        assert m.total_skus == 4
        assert m.matched_skus == 2
        assert m.coverage_pct == 50.0
        assert m.matched_groups == 1
        assert m.back_in_stock_opportunities == 1
        assert m.back_in_stock_skus == 1

    def test_back_in_stock_requires_mixed_stock(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M", in_stock=True),
            sku("b", title="Chase Tee", colour="Black", size="M", in_stock=True),
        )
        m = compute_metrics(s)
        assert m.matched_skus == 2
        assert m.back_in_stock_opportunities == 0

    def test_by_shop_and_brand_breakdowns(self):
        s = norm(
            sku("a", brand="Carhartt WIP", title="Chase Tee", colour="Black",
                size="M", in_stock=True),
            sku("b", brand="Carhartt WIP", title="Chase Tee", colour="Black",
                size="M", in_stock=False),
            sku("a", brand="Veja", title="V-10", colour="White", size="41"),
        )
        m = compute_metrics(s)
        assert m.by_shop["a"]["total_skus"] == 2
        assert m.by_shop["a"]["matched_skus"] == 1
        assert m.by_shop["b"]["matched_skus"] == 1
        assert m.by_shop["a"]["back_in_stock_opportunities"] == 1
        assert m.by_brand["carhartt wip"]["matched_skus"] == 2
        assert m.by_brand["carhartt wip"]["back_in_stock_opportunities"] == 1
        assert m.by_brand["veja"]["matched_skus"] == 0

    def test_empty_input(self):
        m = compute_metrics([])
        assert m.total_skus == 0
        assert m.coverage_pct == 0.0
        assert m.to_dict()["algorithm_version"]


# ---------------------------------------------------------------------------
# Handle-derived colour extraction (v1.2.0)
# ---------------------------------------------------------------------------

_PROD_VOCAB = vocab(
    "tobacco", "blue", "rinsed", "marble", "used", "wash", "washed",
    "obsidian", "stone", "black", "raven", "white", "de", "fleur", "sel",
)


class TestHandleColourExtraction:
    def test_derives_trailing_colour_word(self):
        assert derive_colour_from_handle(
            "Carhartt WIP Arizona Jacket",
            "https://x.com/products/carhartt-wip-arizona-jacket-tobacco",
            _PROD_VOCAB,
        ) == "tobacco"

    def test_strips_trailing_disambiguation_digit(self):
        assert derive_colour_from_handle(
            "Carhartt WIP Arizona Jacket",
            "https://x.com/products/carhartt-wip-arizona-jacket-tobacco-2",
            _PROD_VOCAB,
        ) == "tobacco"

    def test_multi_word_colour(self):
        assert derive_colour_from_handle(
            "Carhartt WIP Firth Sweater",
            "https://x.com/products/carhartt-wip-firth-sweater-fleur-de-sel",
            _PROD_VOCAB,
        ) == "fleur de sel"

    def test_unknown_word_is_ambiguous(self):
        # "special" is not a known colour word -> refuse to infer anything.
        assert derive_colour_from_handle(
            "Carhartt WIP Arizona Jacket",
            "https://x.com/products/carhartt-wip-arizona-jacket-special-edition",
            _PROD_VOCAB,
        ) == ""

    def test_style_code_handle_derives_nothing(self):
        # Sneakersnstuff-style handle: ends in a manufacturer style code.
        assert derive_colour_from_handle(
            "Carhartt WIP Detroit Jacket",
            "https://x.com/products/carhartt-wip-detroit-jacket-i032940",
            _PROD_VOCAB,
        ) == ""

    def test_numeric_colour_code_derives_nothing(self):
        # Biffi-style handle: a colour *code*, not a colour word.
        assert derive_colour_from_handle(
            "Carhartt WIP Detroit Jacket",
            "https://x.com/products/carhartt-wip-detroit-jacket-890r",
            vocab("890r"),
        ) == ""

    def test_title_absent_from_handle_derives_nothing(self):
        assert derive_colour_from_handle(
            "Carhartt WIP Arizona Jacket",
            "https://x.com/products/some-other-thing-blue",
            _PROD_VOCAB,
        ) == ""

    def test_colour_already_in_title_is_not_rederived(self):
        assert derive_colour_from_handle(
            "Harlem Cap, Raven / White",
            "https://x.com/products/harlem-cap-raven-white-raven-white",
            _PROD_VOCAB,
        ) == ""

    def test_empty_vocabulary_never_derives(self):
        assert derive_colour_from_handle(
            "Carhartt WIP Arizona Jacket",
            "https://x.com/products/carhartt-wip-arizona-jacket-tobacco",
            frozenset(),
        ) == ""

    def test_empty_url_never_derives(self):
        assert derive_colour_from_handle("Arizona Jacket", "", _PROD_VOCAB) == ""

    def test_build_vocabulary_drops_placeholders_and_short_tokens(self):
        v = build_colour_vocabulary([
            {"colour": "Black / Gold"},
            {"colour": "No Colour Listed"},
            {"colour": "a"},
        ])
        assert "black" in v and "gold" in v
        assert "no" not in v and "listed" not in v and "colour" not in v
        assert "a" not in v


class TestNormalizeWithVocabulary:
    def test_scraped_colour_wins_over_handle(self):
        s = normalize_sku(
            sku("a", title="Arizona Jacket", colour="Black", size="M",
                url="https://x.com/products/arizona-jacket-blue"),
            0, vocab("blue", "black"),
        )
        assert s.colour == frozenset({"black"})
        assert s.derived_colour == ""

    def test_derived_colour_used_when_field_empty(self):
        s = normalize_sku(
            sku("a", title="Carhartt WIP Arizona Jacket",
                url="https://x.com/products/carhartt-wip-arizona-jacket-tobacco"),
            0, vocab("tobacco"),
        )
        assert s.colour == frozenset({"tobacco"})
        assert s.derived_colour == "tobacco"

    def test_composite_key_ignores_derived_colour(self):
        # The composite anchor must stay based on scraped data only, so a
        # derived colour can never move a SKU away from a previously valid match.
        s = normalize_sku(
            sku("a", title="Carhartt WIP Arizona Jacket",
                url="https://x.com/products/carhartt-wip-arizona-jacket-tobacco"),
            0, vocab("tobacco"),
        )
        assert s.composite_key == "arizona jacket"


class TestDerivedColourCompatibility:
    def _derived(self, shop, word, title="Landon Pant"):
        slug = title.lower().replace(" ", "-")
        return normalize_sku(
            sku(shop, title=f"Carhartt WIP {title}",
                url=f"https://x.com/products/carhartt-wip-{slug}-{word}"),
            0, _PROD_VOCAB,
        )

    def test_derived_equals_plain_same_colour(self):
        a = self._derived("afew", "blue")
        b = normalize_sku(
            sku("ag", title="Landon Pant", colour="Blue", size="M"), 0,
            _PROD_VOCAB,
        )
        assert colour_compatible(a, b)

    def test_derived_does_not_subset_plain_superset(self):
        # A bare derived "blue" must not match "Blue Rinsed" (subsets are only
        # for scraped colours; derived evidence requires equality).
        a = self._derived("afew", "blue")
        b = normalize_sku(
            sku("ag", title="Landon Pant", colour="Blue Rinsed", size="M"), 0,
            _PROD_VOCAB,
        )
        assert not colour_compatible(a, b)

    def test_plain_subset_rule_unchanged(self):
        a = normalize_sku(sku("a", title="X", colour="Black", size="M"), 0)
        b = normalize_sku(sku("b", title="X", colour="Black Rinsed", size="M"), 0)
        assert colour_compatible(a, b)


class TestDerivedColourClustering:
    def test_derived_colour_bridges_two_shops(self):
        v = vocab("blue", "black")
        records = [
            sku("afew", title="Carhartt WIP Belmont Pant", size="M",
                url="https://x.com/products/carhartt-wip-belmont-pant-blue"),
            sku("dtc", title="Belmont Pant", colour="Blue", size="M"),
        ]
        s = [normalize_sku(r, i, v) for i, r in enumerate(records)]
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"afew", "dtc"}

    def test_bare_derived_colour_does_not_pick_between_siblings(self):
        v = vocab("blue", "rinsed", "marble", "used", "wash")
        records = [
            sku("afew", title="Carhartt WIP Landon Pant", size="M",
                url="https://x.com/products/carhartt-wip-landon-pant-blue"),
            sku("ag", title="Carhartt WIP Landon Pant", colour="Blue Rinsed",
                size="M"),
            sku("ag", title="Carhartt WIP Landon Pant",
                colour="Blue Marble Used Wash", size="M"),
        ]
        s = [normalize_sku(r, i, v) for i, r in enumerate(records)]
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert all("afew" not in c.shops for c in cross)

    def test_derived_member_does_not_orphan_plain_colourway(self):
        # Regression: adding the derived colour must not split the two plain
        # members (cw + ag) into an inferior group and drop them.
        v = vocab("obsidian", "stone", "washed")
        records = [
            sku("cw", title="Parrish Pant", colour="Obsidian", size="M"),
            sku("ag", title="Carhartt WIP Parrish Pant",
                colour="Obsidian Stone Washed", size="M"),
            sku("afew", title="Carhartt WIP Parrish Pant", size="M",
                url="https://x.com/products/carhartt-wip-parrish-pant-obsidian"),
        ]
        s = [normalize_sku(r, i, v) for i, r in enumerate(records)]
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"cw", "ag", "afew"}

    def test_redundant_handle_colour_keeps_title_match(self):
        v = vocab("raven", "white")
        records = [
            sku("dtc", title="Harlem Cap", colour="Raven White",
                size="ONE_SIZE"),
            sku("beamhill", title="Carhartt WIP Harlem Cap, Raven / White",
                size="ONE_SIZE",
                url="https://x.com/products/carhartt-wip-harlem-cap-raven-white-raven-white"),
        ]
        s = [normalize_sku(r, i, v) for i, r in enumerate(records)]
        assert s[1].derived_colour == ""
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"dtc", "beamhill"}

    def test_identical_derived_colours_match_across_shops(self):
        v = vocab("black")
        records = [
            sku("afew", title="Carhartt WIP XT 6 Sneakers", size="42",
                url="https://x.com/products/carhartt-wip-xt-6-sneakers-black"),
            sku("biffi", title="Carhartt WIP XT 6 Sneakers", size="42",
                url="https://y.com/products/carhartt-wip-xt-6-sneakers-black"),
        ]
        s = [normalize_sku(r, i, v) for i, r in enumerate(records)]
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"afew", "biffi"}


# ---------------------------------------------------------------------------
# Salomon cross-system (UK store vs EU retailers)
# ---------------------------------------------------------------------------

class TestSalomonCrossSystemClustering:
    def test_uk_store_matches_eu_retailer(self):
        records = [
            sku("salomon", brand="Salomon", title="XT 6", colour="Black",
                size="8.5 UK"),
            sku("asphaltgold", brand="Salomon", title="Salomon XT 6",
                colour="Black", size="42 2/3"),
        ]
        s = [normalize_sku(r, i) for i, r in enumerate(records)]
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"salomon", "asphaltgold"}

    def test_non_mapped_salomon_size_still_does_not_match(self):
        records = [
            sku("salomon", brand="Salomon", title="XT 6", colour="Black",
                size="8.5 UK"),
            sku("asphaltgold", brand="Salomon", title="Salomon XT 6",
                colour="Black", size="43 1/3"),
        ]
        s = [normalize_sku(r, i) for i, r in enumerate(records)]
        assert [c for c in build_clusters(s) if c.is_cross_shop] == []


# ---------------------------------------------------------------------------
# Matched-group sampling (dashboard detail list)
# ---------------------------------------------------------------------------

class TestMatchedGroupSamples:
    def _clusters(self):
        s = norm(
            sku("a", title="Chase Tee", colour="Black", size="M", in_stock=True),
            sku("b", title="Chase Tee", colour="Black", size="M", in_stock=True),
            sku("a", title="Solo Cap", colour="Red", size="ONE_SIZE", in_stock=True),
            sku("b", title="Solo Cap", colour="Red", size="ONE_SIZE", in_stock=False),
        )
        return build_clusters(s)

    def test_mixed_only_skips_same_stock_groups(self):
        clusters = self._clusters()
        assert len(matched_group_samples(clusters, limit=None, mixed_only=False)) == 2
        mixed = matched_group_samples(clusters, limit=None, mixed_only=True)
        assert len(mixed) == 1
        assert mixed[0]["has_mixed_stock"] is True

    def test_cap_keeps_only_opportunities(self):
        clusters = self._clusters()
        mixed = matched_group_samples(clusters, limit=1, mixed_only=True)
        assert len(mixed) == 1
        assert mixed[0]["has_mixed_stock"] is True


# ---------------------------------------------------------------------------
# SSENSE colour-prefixed titles + size systems (v1.4.0; guard v1.4.1)
# ---------------------------------------------------------------------------

class TestSSENSEPrefixedTitles:
    def test_prefixed_title_matches_plain_title_same_colour(self):
        # SSENSE writes the colour first; DTC keeps it in the colour field.
        a, b = norm(
            sku("ssense", title="Black OG Active Jacket", colour="Black", size="M"),
            sku("carhartt_wip", title="OG Active Jacket", colour="Black", size="M"),
        )
        assert skus_match(a, b)

    def test_prefixed_multiword_colour_matches_subset(self):
        # SSENSE prefixes a descriptive colour; the DTC keeps the core words.
        a, b = norm(
            sku("ssense", title="Dusty Hamilton Brown OG Active Jacket",
                colour="Dusty hamilton brown", size="M"),
            sku("carhartt_wip", title="OG Active Jacket",
                colour="Hamilton Brown", size="M"),
        )
        assert skus_match(a, b)

    def test_prefixed_title_different_colour_rejected(self):
        a, b = norm(
            sku("ssense", title="Black OG Active Jacket", colour="Black", size="M"),
            sku("carhartt_wip", title="OG Active Jacket", colour="Blue", size="M"),
        )
        assert not skus_match(a, b)

    def test_prefixed_title_colourless_plain_side_rejected(self):
        # An empty colour is still not a wildcard, even via the base-key path.
        a, b = norm(
            sku("ssense", title="Black OG Active Jacket", colour="Black", size="M"),
            sku("carhartt_wip", title="OG Active Jacket", colour="", size="M"),
        )
        assert not skus_match(a, b)

    def test_non_prefixed_titles_do_not_use_base_key(self):
        # Regression (precision): without a colour-prefixed side, removing colour
        # words must NOT create a match. Veja "V-12 Leather White" and asphaltgold
        # "V-12 Leather" + "Extra White / Camel" are different colourways.
        a, b = norm(
            sku("veja", brand="Veja", title="V-12 LEATHER WHITE", colour="White",
                size="43"),
            sku("asphaltgold", brand="Veja", title="Veja V-12 Leather",
                colour="Extra White / Camel", size="43"),
        )
        assert not skus_match(a, b)
        assert not a.colour_prefixed and not b.colour_prefixed

    def test_generic_tail_trim_enables_sneaker_match(self):
        # SSENSE appends "Sneakers"; DTC keeps the colour in the title.
        a, b = norm(
            sku("ssense", brand="Veja", title="White V-90 Leather Sneakers",
                colour="White", size="40"),
            sku("veja", brand="Veja", title="V-90 LEATHER WHITE BLACK",
                colour="White / Black", size="40"),
        )
        assert skus_match(a, b)

    def test_ssense_waist_size_matches_bare_trouser_size(self):
        a, b = norm(
            sku("ssense", title="Blue Simple Pant Jeans", colour="Blue",
                size="WAIST US 28 x 32"),
            sku("carhartt_wip", title="Simple Pant", colour="Blue", size="28/32"),
        )
        assert skus_match(a, b)

    def test_ssense_it_size_matches_bare_shoe_size(self):
        a, b = norm(
            sku("ssense", brand="Veja", title="White V-90 Leather Sneakers",
                colour="White", size="IT 40"),
            sku("veja", brand="Veja", title="V-90 LEATHER WHITE BLACK",
                colour="White / Black", size="40"),
        )
        assert skus_match(a, b)

    def test_ssense_different_waist_size_rejected(self):
        a, b = norm(
            sku("ssense", title="Blue Simple Pant Jeans", colour="Blue",
                size="WAIST US 28 x 30"),
            sku("carhartt_wip", title="Simple Pant", colour="Blue", size="28/32"),
        )
        assert not skus_match(a, b)

    def test_clustering_links_ssense_with_two_shops(self):
        records = [
            sku("ssense", title="Black OG Active Jacket", colour="Black", size="M"),
            sku("carhartt_wip", title="OG Active Jacket", colour="Black", size="M"),
            sku("asphaltgold", title="Carhartt WIP OG Active Jacket",
                colour="Black Stone Canvas", size="M"),
        ]
        s = norm(*records)
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"ssense", "carhartt_wip", "asphaltgold"}


# ---------------------------------------------------------------------------
# base-key precision guard (v1.4.1): the prefixed side's *leading* colour must
# be corroborated by the other side's structured colour, not just a shared
# accent word. Regression for the "Prescott Coat" false positive.
# ---------------------------------------------------------------------------

class TestPrefixedLeadingColourGuard:
    def test_prescott_accent_colour_rejected(self):
        # Confirmed false positive (as shipped in 1.4.0): SSENSE prefixes the
        # colour "Green" (colour "Oxide green/Black"); DTC is "Black / Black".
        # The subset rule alone accepts them via the shared accent "black", but
        # the leading colour differs -> different colourways, must not match.
        a, b = norm(
            sku("ssense", title="Green Prescott Coat",
                colour="Oxide green/Black", size="M"),
            sku("carhartt_wip", title="Prescott Coat",
                colour="Black / Black", size="M"),
        )
        assert a.colour_prefixed and not b.colour_prefixed
        assert a.base_key == b.base_key == "prescott coat"
        assert not skus_match(a, b)

    def test_leading_colour_on_other_side_still_matches(self):
        # The guard must not reject a genuinely colour-compatible pair: SSENSE
        # leads with "Green" and the other shop's colour corroborates it.
        a, b = norm(
            sku("ssense", title="Green Prescott Coat",
                colour="Oxide green/Black", size="M"),
            sku("carhartt_wip", title="Prescott Coat", colour="Green", size="M"),
        )
        assert skus_match(a, b)

    def test_prefixed_leading_colour_reversed_order(self):
        # Symmetric: the guard applies to whichever side is colour-prefixed.
        a, b = norm(
            sku("carhartt_wip", title="Prescott Coat",
                colour="Black / Black", size="M"),
            sku("ssense", title="Green Prescott Coat",
                colour="Oxide green/Black", size="M"),
        )
        assert not skus_match(a, b)

    def test_subset_variation_with_shared_leading_colour_still_matches(self):
        # Legitimate variation: SSENSE leads with "Blue" and adds wash
        # descriptors; the plain side publishes "Blue". Must keep matching.
        a, b = norm(
            sku("ssense", title="Blue Simple Pant Jeans",
                colour="Blue Stone Washed", size="WAIST US 28 x 32"),
            sku("carhartt_wip", title="Simple Pant", colour="Blue", size="28/32"),
        )
        assert skus_match(a, b)


# ---------------------------------------------------------------------------
# Size-system distinctions (guards must not conflate systems)
# ---------------------------------------------------------------------------

class TestSizeSystemDistinctions:
    def test_ssense_single_waist_not_equivalent_to_w_prefix(self):
        # "WAIST US 28" -> "28"; a W-prefixed waist "W29" stays "w29" and the
        # two must not be treated as the same size.
        ssense = normalize_sku(sku("ssense", title="Simple Pant",
            colour="Blue", size="WAIST US 28"), 0)
        footway = normalize_sku(sku("footway", title="Simple Pant",
            colour="Blue", size="W29"), 0)
        assert ssense.size_key == "28"
        assert footway.size_key == "w29"
        assert not skus_match(ssense, footway)

    def test_inseam_remains_distinct_from_waist_only(self):
        assert normalize_size("W29", "carhartt wip") == "w29"
        assert normalize_size("W29L32", "carhartt wip") == "w29l32"
        assert normalize_size("W29", "carhartt wip") != normalize_size(
            "W29L32", "carhartt wip")

    def test_ssense_waist_with_inseam_keeps_inseam(self):
        assert normalize_size("WAIST US 34 x 34", "carhartt wip") == "3434"
        assert normalize_size("WAIST US 34 x 32", "carhartt wip") == "3432"


# ---------------------------------------------------------------------------
# base-key safety: generic-tail trimming and distinct models
# ---------------------------------------------------------------------------

class TestBaseKeySafety:
    def test_generic_tail_trim_does_not_merge_distinct_models(self):
        # "Sneakers" is trimmed from the base key, but two distinct models must
        # not merge merely because both end in a generic noun.
        a = normalize_sku(sku("ssense", brand="Veja",
            title="White V-90 Leather Sneakers", colour="White", size="40"), 0)
        b = normalize_sku(sku("veja", brand="Veja",
            title="V-15 Leather Sneakers", colour="White", size="40"), 0)
        assert base_product_key(a.title_key, a.colour) == "v 90 leather"
        assert base_product_key(b.title_key, b.colour) == "v 15 leather"
        assert not skus_match(a, b)

    def test_generic_tail_trim_requires_same_model_stem(self):
        # Trimming "Jeans" aligns SSENSE with the plain title, but must not
        # align it with a different product that shares the same colour.
        a = normalize_sku(sku("ssense", title="Blue Simple Pant Jeans",
            colour="Blue", size="WAIST US 28 x 32"), 0)
        b = normalize_sku(sku("carhartt_wip", title="Simple Shirt",
            colour="Blue", size="28/32"), 0)
        assert not skus_match(a, b)

    def test_distinct_models_with_similar_base_do_not_match(self):
        a = normalize_sku(sku("ssense", title="Black OG Active Jacket",
            colour="Black", size="M"), 0)
        b = normalize_sku(sku("carhartt_wip", title="OG Active Vest",
            colour="Black", size="M"), 0)
        assert a.base_key == "og active jacket"
        assert b.base_key == "og active vest"
        assert not skus_match(a, b)


# ---------------------------------------------------------------------------
# Regressions from the audit: bridged clusters and verified SSENSE matches
# ---------------------------------------------------------------------------

class TestBridgedClusterRegressions:
    def test_colourless_and_derived_retailers_join_bridged_cluster(self):
        # Reproduces the audited OG Detroit cluster: the SSENSE bridge plus the
        # composite link must pull in the derived-colour (afew) and colourless
        # (xtreme) retailers as one resolved colourway -- the six legitimate
        # non-SSENSE pairs -- without bridging two colourways.
        v = vocab("black", "canvas", "stone")
        records = [
            sku("ssense", title="Black OG Detroit Jacket", colour="Black",
                size="L"),
            sku("asphaltgold", title="Carhartt WIP OG Detroit Jacket",
                colour="Black / Black Stone Canvas", size="L"),
            sku("afew", title="Carhartt WIP OG Detroit Jacket", size="L",
                url="https://x.com/products/carhartt-wip-og-detroit-jacket-black-black"),
            sku("xtreme",
                title="Carhartt WIP OG Detroit Jacket Black/Black stone canvas",
                size="L"),
        ]
        s = [normalize_sku(r, i, v) for i, r in enumerate(records)]
        cross = [c for c in build_clusters(s) if c.is_cross_shop]
        assert len(cross) == 1
        assert cross[0].shops == {"ssense", "asphaltgold", "afew", "xtreme"}

    def test_verified_ssense_shapes_still_match(self):
        cases = [
            # Simple Pant (WAIST US x inseam)
            (sku("ssense", title="Blue Simple Pant Jeans", colour="Blue",
                 size="WAIST US 28 x 32"),
             sku("carhartt_wip", title="Simple Pant", colour="Blue",
                 size="28/32")),
            # V-90 (IT size + generic tail)
            (sku("ssense", brand="Veja", title="White V-90 Leather Sneakers",
                 colour="White", size="IT 40"),
             sku("veja", brand="Veja", title="V-90 LEATHER WHITE BLACK",
                 colour="White / Black", size="40")),
            # OG Active Jacket
            (sku("ssense", title="Black OG Active Jacket", colour="Black",
                 size="M"),
             sku("carhartt_wip", title="OG Active Jacket", colour="Black",
                 size="M")),
            # OG Detroit Jacket
            (sku("ssense", title="Black OG Detroit Jacket", colour="Black",
                 size="L"),
             sku("carhartt_wip", title="OG Detroit Jacket", colour="Black",
                 size="L")),
        ]
        for pair in cases:
            a, b = norm(*pair)
            assert skus_match(a, b)
