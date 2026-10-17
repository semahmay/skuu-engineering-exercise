"""Value normalization for cross-shop product matching.

All functions here are pure and side-effect free so they can be unit-tested
without a database or network. They turn raw SKU fields (which differ wildly
between shops) into canonical keys that can be compared safely.

Design rules (see ``docs`` in ``matcher.py``):

- Missing/empty values are **never** used as matching identifiers. Each
  ``normalize_*`` function returns ``""`` for empty input, and the matcher
  refuses to match on empty keys.
- Normalization must be *conservative*: it is better to miss a match than to
  merge two different products. We therefore do not attempt fuzzy colour
  translation, and cross-system size conversion (e.g. UK -> EU) is done only
  for the single documented Salomon case where the brand's own store publishes
  UK sizes and EU retailers publish EU sizes (see ``normalize_size``).
"""

import re
from typing import Iterable, Mapping

# Brand tokens that shops prepend to titles (e.g. "Carhartt WIP OG Detroit").
BRAND_TOKENS = frozenset({"carhartt", "wip", "salomon", "veja"})

# Trailing product nouns that one shop may append to a title but another omits
# ("V-90 Leather" vs "V-90 Leather Sneakers"). They never identify a product, so
# they are trimmed from the base product key (``base_product_key``).
GENERIC_PRODUCT_TAIL = frozenset({"sneakers", "shoe", "shoes", "trainers", "jeans"})


def canonical_brand(brand: str) -> str:
    """Canonical, lowercase brand name (empty stays empty)."""
    b = (brand or "").strip().lower()
    if b in ("carhartt wip", "carhartt"):
        return "carhartt wip"
    return b


def normalize_title(title: str) -> str:
    """Canonical product title.

    Lowercases, drops ``*marketing*`` tags, converts punctuation to spaces and
    strips any leading brand tokens. E.g.::

        "Carhartt WIP OG Detroit Jacket" -> "og detroit jacket"
        "XT-4 OG GTX *Protective*"       -> "xt 4 og gtx"
        "Women's L/S Tee"                -> "women s l s tee"
    """
    text = (title or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"\*[^*]*\*", " ", text)          # drop *marketing* tags
    text = re.sub(r"[^a-z0-9]+", " ", text)         # punctuation -> space
    tokens = text.split()
    while tokens and tokens[0] in BRAND_TOKENS:
        tokens.pop(0)
    return " ".join(tokens)


def normalize_colour(colour: str) -> str:
    """Canonical colour string (lowercased, punctuation collapsed). Empty ->
    ``""`` so it is never mistaken for a real colour."""
    return " ".join(re.split(r"[^a-z0-9]+", (colour or "").lower())).strip()


def colour_tokens(colour: str) -> frozenset:
    """Word-level colour tokens.

    Shops name colours differently and inconsistently (``"Black / Black"``,
    ``"Barchan"``, ``"Barchan Stone Dyed"``). Comparing *words* lets the shared
    colour word provide evidence while a genuinely different colourway
    (``"Black"`` vs ``"Hamilton Brown"``) shares nothing and is rejected.
    """
    return frozenset(t for t in re.split(r"[^a-z0-9]+", (colour or "").lower()) if t)


def is_colour_prefixed(title_key: str, colour: Iterable[str]) -> bool:
    """Whether a normalized title *begins* with one of its own colour words.

    Some shops (notably SSENSE) fold the colourway into the title as a leading
    prefix — ``"Black OG Active Jacket"`` — while others keep the colour in a
    separate field (``"OG Active Jacket"`` + ``"Black"``). Detecting that shape
    lets ``base_product_key`` bridge the two naming conventions *only* when a
    colour-prefixed title is actually present, so shops that never prefix their
    titles are left untouched.
    """
    words = title_key.split()
    return bool(words) and words[0] in frozenset(colour)


def leading_colour_run(title_key: str, colour: Iterable[str]) -> frozenset:
    """The leading, contiguous run of title words that are the SKU's own colour.

    The colour a shop prefixes onto a title is its *headline* colour, but it is
    not always a single word: SSENSE writes ``"Dusty Hamilton Brown OG Active
    Jacket"`` (colour ``"Dusty hamilton brown"``) as well as the one-word
    ``"Green Prescott Coat"`` (colour ``"Oxide green/Black"``). Returning the
    whole leading run — not just the first token — lets the matcher distinguish
    "the headline colourway" from "a word that merely appears later in the
    colour string", so a genuine multi-word colour match is not rejected while a
    shared *accent* word (``green`` vs a ``Black``-only colour) still is.

    Only contiguous words from the start of the title that are in ``colour`` are
    returned; the run stops at the first non-colour word. Empty when the title
    is empty or does not start with a colour word.
    """
    own = frozenset(colour)
    run = []
    for word in title_key.split():
        if word in own:
            run.append(word)
        else:
            break
    return frozenset(run)


def base_product_key(title_key: str, colour: Iterable[str]) -> str:
    """Title with its own colour words removed and generic nouns trimmed.

    This is the identity a colour-prefixed title should have shared with a plain
    title all along: ``"black og active jacket"`` (colour ``black``) and
    ``"og active jacket"`` both reduce to ``"og active jacket"``. Only the SKU's
    **own** colour tokens are removed (never the whole colour vocabulary, which
    contains product words), and only a trailing generic noun is trimmed, so a
    different product that merely shares an accent word is not merged.
    """
    own = frozenset(colour)
    words = [w for w in title_key.split() if w not in own]
    while words and words[-1] in GENERIC_PRODUCT_TAIL:
        words.pop()
    return " ".join(words)


# Tokens that occur in some shops' colour strings but carry no colour meaning.
# They are dropped from the derived-colour vocabulary so a handle word (or a
# placeholder like "no colour listed") can never be mistaken for a colour.
COLOUR_VOCABULARY_STOPWORDS = frozenset({"no", "not", "color", "colour", "listed", "n"})


def build_colour_vocabulary(records: Iterable[Mapping]) -> frozenset:
    """Collect the colour words that shops actually publish.

    The vocabulary is the union of every non-empty ``colour`` field in the
    dataset. Handle-derived colour (``derive_colour_from_handle``) is accepted
    only when *every* candidate word is in this vocabulary, which is what stops
    arbitrary URL words from ever becoming a colour. It deliberately contains no
    product/brand words because it is built from colour fields only.
    """
    vocab = set()
    for record in records:
        vocab |= colour_tokens(record.get("colour"))
    return frozenset(
        t
        for t in vocab
        if len(t) >= 2 and not t.isdigit() and t not in COLOUR_VOCABULARY_STOPWORDS
    )


# Handle fragments that are identifiers, not colour: manufacturer style codes
# (``i032297``, ``l49160900``) and numeric colour codes (``890r``, ``3tfxx``).
_STYLE_CODE = re.compile(r"^[a-z]{1,3}\d{3,}[a-z0-9]*$")
_STYLE_CODE_NUMERIC = re.compile(r"^\d{3,}[a-z]{0,3}$")
_TRAILING_DIGITS = re.compile(r"\d+$")


def _handle_slug(url: str) -> str:
    """Last path segment of a product URL, without query/fragment/trailing slash."""
    slug = (url or "").split("?")[0].split("#")[0].rstrip("/")
    return slug.split("/")[-1] if slug else ""


def derive_colour_from_handle(title: str, url: str, vocabulary: Iterable[str]) -> str:
    """Derive a colourway from a product URL handle, conservatively.

    Some retailers only publish the colourway in the URL handle, e.g.
    ``.../carhartt-wip-arizona-jacket-tobacco`` for a product titled
    ``Carhartt WIP Arizona Jacket``. We locate the product title inside the
    handle as an in-order token subsequence and treat only the *trailing* tokens
    as a colour candidate. The candidate is accepted **only** when every
    remaining word is a known colour word (``build_colour_vocabulary``);
    disambiguation digits and style codes are stripped first. A handle that does
    not contain the title, or that yields an unknown word, gives ``""`` — a
    missing colour is never inferred and never becomes a wildcard.
    """
    vocab = frozenset(vocabulary or ())
    if not vocab:
        return ""
    handle_tokens = [
        t for t in re.split(r"[^a-z0-9]+", _handle_slug(url).lower()) if t
    ]
    title_tokens = normalize_title(title).split()
    if not handle_tokens or not title_tokens:
        return ""

    # Find where the product title ends inside the handle.
    pos = 0
    for token in title_tokens:
        while pos < len(handle_tokens) and handle_tokens[pos] != token:
            pos += 1
        if pos == len(handle_tokens):
            return ""  # title not present in the handle -> do not guess
        pos += 1
    remainder = handle_tokens[pos:]
    if not remainder:
        return ""

    words: list = []
    for token in remainder:
        if token.isdigit():  # trailing disambiguation number ("-2", "-3")
            continue
        if _STYLE_CODE.match(token) or _STYLE_CODE_NUMERIC.match(token):
            continue  # manufacturer / colour code, not a colour word
        word = _TRAILING_DIGITS.sub("", token)  # "washed1" -> "washed"
        if not word:
            continue
        if word not in vocab:
            return ""  # unknown word -> ambiguous, refuse to infer a colour
        words.append(word)
    if not words:
        return ""
    # When the title already spells out the colour, the handle merely repeats it
    # (e.g. title "Harlem Cap, Raven / White" + handle "...-raven-white-raven-
    # white"). Re-deriving would double the colour and break existing title-based
    # matches, so treat an already-present colour as "no derivation".
    title_words = set(title_tokens)
    if all(word in title_words for word in words):
        return ""
    return " ".join(words)


# Size synonyms that all mean "one size" once units/marks are stripped. Italian
# shops (Biffi) label accessories ``TU``/``Taglia Unica``; the Veja store uses a
# dotted ``O.S``; both must land on the same token as the ``One Size`` that the
# DTC stores publish.
_ONE_SIZE_COMPACT = frozenset(
    {
        "os", "one", "onesize", "osfa", "ns", "uni", "universal", "u",
        "tu", "unica", "tagliaunica", "onesizefitsall",
    }
)

# Salomon publishes footwear in different size systems per region: the AU/NZ
# store exposes explicit UK sizes while EU retailers use EU sizes (with 1/3
# steps). This explicit, brand-scoped table (standard Salomon men's/unisex
# chart) lets the two systems meet *only* for Salomon footwear and only when one
# side is unambiguously labelled "UK". Everywhere else no conversion happens, so
# the conservative default is preserved. Keys/values are raw UK/EU strings; the
# EU values are normalized through ``normalize_size`` so they align with the
# retailer keys (e.g. ``"38 2/3"`` -> ``"3823"``).
_SALOMON_UK_TO_EU = {
    "3.5": "36", "4": "36 2/3", "4.5": "37 1/3", "5": "38", "5.5": "38 2/3",
    "6": "39 1/3", "6.5": "40", "7": "40 2/3", "7.5": "41 1/3", "8": "42",
    "8.5": "42 2/3", "9": "43 1/3", "9.5": "44", "10": "44 2/3",
    "10.5": "45 1/3", "11": "46", "11.5": "46 2/3", "12": "47 1/3",
    "12.5": "48", "13": "48 2/3", "13.5": "49 1/3",
}

_SALOMON_UK_COMPACT = re.compile(r"^([0-9]+(?:\.[0-9]+)?)uk$")

# SSENSE labels trouser and shoe sizes with explicit system prefixes: a
# ``WAIST US 28 x 32`` trouser becomes ``waistus28x32`` and an ``IT 40`` shoe
# becomes ``it40``. Retailers publish the numeric part bare (``28/32`` ->
# ``2832``, ``40`` -> ``40``), so stripping these *unique* prefixes is what lets
# the two systems meet. The patterns cannot fire for any other shop's sizes.
_SSENSE_WAIST = re.compile(r"^waistus([0-9]+)x([0-9]+)$")
_SSENSE_WAIST_SINGLE = re.compile(r"^waistus([0-9]+)$")
_SSENSE_IT = re.compile(r"^it([0-9]+(?:\.[0-9]+)?)$")


def _salomon_uk_size_to_eu(compact: str) -> str:
    """Map a compact Salomon UK size (``"5.5uk"``) to its EU key, else ``""``."""
    match = _SALOMON_UK_COMPACT.match(compact)
    if not match:
        return ""
    eu = _SALOMON_UK_TO_EU.get(match.group(1))
    return normalize_size(eu) if eu else ""


def normalize_size(size: str, brand: str = "") -> str:
    """Canonical size key.

    - trims and lowercases;
    - removes parentheticals (``"S (53-56cm)"`` -> ``"s"``);
    - collapses whitespace and joins number+unit (``"10.5  UK"`` -> ``"10.5uk"``)
      so ``"3.5 UK"``, ``"3.5  UK"`` and ``"3.5 Uk"`` are equal;
    - maps one-size synonyms (``O.S``, ``OSFA``, ``N/S``, ``One Size`` ...) to a
    single token ``ONE_SIZE``;
    - strips SSENSE's explicit system prefixes ``WAIST US`` and ``IT`` so a
    trouser ``"WAIST US 28 x 32"`` (``"waistus28x32"``) becomes ``"2832"`` and a
    shoe ``"IT 40"`` (``"it40"``) becomes ``"40"``, matching the bare numeric
    sizes other shops publish (see ``_SSENSE_WAIST``/``_SSENSE_IT``).

    Returns ``""`` for empty input. It does **not** convert between size systems
    in general — the single exception is an explicitly ``UK`` Salomon size, which
    is mapped to its EU equivalent so the AU/NZ store can be compared with EU
    retailers (see ``_SALOMON_UK_TO_EU``). Pass the (canonical) ``brand`` to
    enable that; without it the behaviour is byte-identical.
    """
    text = (size or "").strip().lower()
    text = re.sub(r"\([^)]*\)", " ", text)
    tokens = [t for t in re.sub(r"[^a-z0-9.]+", " ", text).split() if t]
    if not tokens:
        return ""
    compact = "".join(tokens)
    # ``O.S``/``T.U`` keep their inner dot through tokenization (dots are kept so
    # ``7.5`` stays a decimal), so compare the dot-stripped form too.
    if compact in _ONE_SIZE_COMPACT or compact.replace(".", "") in _ONE_SIZE_COMPACT:
        return "ONE_SIZE"
    if canonical_brand(brand) == "salomon":
        eu = _salomon_uk_size_to_eu(compact)
        if eu:
            return eu
    match = _SSENSE_WAIST.match(compact)
    if match:
        return match.group(1) + match.group(2)
    match = _SSENSE_WAIST_SINGLE.match(compact)
    if match:
        return match.group(1)
    match = _SSENSE_IT.match(compact)
    if match:
        return match.group(1)
    return compact


def extract_style_code(barcode: str) -> str:
    """Return a normalized manufacturer style code, or ``""``.

    Some shops put a real style code in the ``barcode`` field (Carhartt WIP:
    ``I037234_1``); others put nothing or a real GTIN. A GTIN (8/12/13/14
    digits) is an EAN/UPC, not a style identifier, so it is ignored here.
    """
    code = (barcode or "").strip().upper()
    if not code:
        return ""
    digits_only = code.replace(" ", "").replace("-", "")
    if digits_only.isdigit() and len(digits_only) in (8, 12, 13, 14):
        return ""
    return code


def title_tokens(normalized_title: str) -> frozenset:
    return frozenset(normalized_title.split())


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
