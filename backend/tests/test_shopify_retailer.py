"""Tests for the shared Shopify retailer core and per-shop configs.

No network: extraction, brand detection, barcode fallback and per-variant stock
mapping are exercised with in-memory Shopify fixtures.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from app.refresh.concurrency import RefreshConcurrency

from app.scrapers import (
    afew,
    beamhill,
    biffi,
    caliroots,
    footway,
    nugnes1920,
    overkillshop,
    sneakersnstuff,
    xtreme,
)
from app.scrapers import shopify_retailer as core
from app.scrapers.shopify_retailer import (
    ShopifyRetailer,
    build_sku_for,
    detect_brand,
    extract_barcode,
    extract_colour,
    extract_size,
    extract_variant_style_code,
    parse_products_for,
    variant_url,
)

RETAILER = ShopifyRetailer(
    shop_id="testshop", base_url="https://shop.test", currency="EUR"
)

SHOP_MODULES = [
    sneakersnstuff,
    afew,
    footway,
    xtreme,
    biffi,
    beamhill,
    caliroots,
    nugnes1920,
    overkillshop,
]
EXPECTED_SHOPS = {
    sneakersnstuff: ("sneakersnstuff", "EUR"),
    afew: ("afew", "EUR"),
    footway: ("footway", "SEK"),
    xtreme: ("xtreme", "EUR"),
    biffi: ("biffi", "EUR"),
    beamhill: ("beamhill", "EUR"),
    caliroots: ("caliroots", "SEK"),
    nugnes1920: ("nugnes1920", "EUR"),
    overkillshop: ("overkillshop", "EUR"),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_product(handle, title, options, variants, vendor=None):
    return {
        "id": 1,
        "handle": handle,
        "title": title,
        "vendor": vendor,
        "options": [
            {"name": name, "position": i + 1, "values": []}
            for i, name in enumerate(options)
        ],
        "variants": variants,
    }


def make_variant(vid, option1="", option2=None, barcode=None, sku=None,
                 price="100.00", available=True):
    return {
        "id": vid,
        "option1": option1,
        "option2": option2,
        "option3": None,
        "barcode": barcode,
        "sku": sku,
        "price": price,
        "available": available,
    }


# ---------------------------------------------------------------------------
# Brand detection
# ---------------------------------------------------------------------------

class TestBrandDetection:
    @pytest.mark.parametrize(
        "vendor,expected",
        [
            ("Carhartt WIP", "Carhartt WIP"),
            ("CARHARTT WIP", "Carhartt WIP"),
            ("Salomon", "Salomon"),
            ("SALOMON", "Salomon"),
            ("Veja", "Veja"),
            ("VEJA", "Veja"),
            ("Nike", None),
            ("", None),
            (None, None),
        ],
    )
    def test_vendor(self, vendor, expected):
        assert detect_brand(vendor) == expected

    def test_title_fallback(self):
        assert detect_brand("Nike", "Salomon XT-6 OG") == "Salomon"

    def test_no_brand(self):
        assert detect_brand("The North Face", "Nuptse Jacket") is None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def make_product_with(options_names, option_values):
    options = [{"name": n} for n in options_names]
    variant = {f"option{i + 1}": v for i, v in enumerate(option_values)}
    return {"options": options}, variant


class TestExtraction:
    @pytest.mark.parametrize("name", ["Size", "size", "Taglia", "Talla", "Taille"])
    def test_size_localized(self, name):
        product, variant = make_product_with([name], ["42"])
        assert extract_size(product, variant) == "42"

    def test_size_one_size_when_missing(self):
        product, variant = make_product_with(["Title"], ["Default Title"])
        assert extract_size(product, variant) == "One Size"

    def test_colour_option(self):
        product, variant = make_product_with(["Color"], ["Black"])
        assert extract_colour(product, variant) == "Black"

    def test_colour_empty_when_no_option(self):
        product, variant = make_product_with(["Size"], ["M"])
        assert extract_colour(product, variant) == ""

    def test_barcode_from_field(self):
        assert extract_barcode({"barcode": "1234567890123"}) == "1234567890123"

    def test_barcode_gtin_sku_fallback(self):
        assert extract_barcode({"barcode": "", "sku": "4064958104131"}) == "4064958104131"

    def test_barcode_non_gtin_sku(self):
        assert extract_barcode({"barcode": None, "sku": "L49306600-5"}) is None

    def test_barcode_does_not_fabricate_gtin_from_compound_sku(self):
        # ``STYLE-SIZE`` / ``STYLE SIZE`` must not collapse into a fake barcode.
        assert extract_barcode({"barcode": None, "sku": "114805-37"}) is None
        assert extract_barcode({"barcode": None, "sku": "114805 37"}) is None
        # A bare numeric code is still accepted as a GTIN fallback.
        assert extract_barcode({"barcode": None, "sku": "4064958104131"}) == "4064958104131"

    def test_style_code_keeps_compound_sku(self):
        # The separator-bearing source value is preserved as a style code ...
        assert extract_variant_style_code({"sku": "114805-37"}) == "114805-37"
        # ... and never leaks into the barcode field (see build_sku test below).

    def test_style_code_captures_variant_sku(self):
        assert extract_variant_style_code({"sku": "I032940_89"}) == "I032940_89"
        assert extract_variant_style_code({"sku": "  L49306600-42  "}) == "L49306600-42"

    def test_style_code_none_when_sku_is_gtin(self):
        # A GTIN-shaped SKU is a barcode, not a manufacturer style code.
        assert extract_variant_style_code({"sku": "4064958104131"}) is None
        assert extract_variant_style_code({"sku": "12345678"}) is None

    def test_style_code_none_when_sku_missing(self):
        assert extract_variant_style_code({}) is None
        assert extract_variant_style_code({"sku": ""}) is None
        assert extract_variant_style_code({"sku": None}) is None

    def test_variant_url(self):
        assert variant_url(RETAILER, "abc", 99) == "https://shop.test/products/abc?variant=99"
        assert variant_url(RETAILER, "abc", None) == "https://shop.test/products/abc"


# ---------------------------------------------------------------------------
# SKU building
# ---------------------------------------------------------------------------

def test_build_sku_maps_fields():
    product = {
        "handle": "salomon-xt-6",
        "vendor": "SALOMON",
        "title": "XT-6",
        "options": [{"name": "Size"}],
    }
    variant = {
        "id": 7,
        "option1": "42",
        "price": "180.00",
        "available": True,
        "barcode": None,
        "sku": "L49306600-42",
    }
    sku = build_sku_for(RETAILER, product, variant)
    assert sku.shop == "testshop"
    assert sku.brand == "Salomon"
    assert sku.title == "XT-6"
    assert sku.size == "42"
    assert sku.colour == ""
    assert sku.price == 180.0
    assert sku.currency == "EUR"
    assert sku.in_stock is True
    assert sku.barcode is None
    assert sku.style_code == "L49306600-42"
    assert sku.url == "https://shop.test/products/salomon-xt-6?variant=7"


def test_build_sku_captures_style_code_but_not_gtin():
    # Manufacturer style code is captured; a GTIN-shaped SKU is not a style code.
    product = {
        "handle": "salomon-xt-6",
        "vendor": "SALOMON",
        "title": "XT-6",
        "options": [{"name": "Size"}],
    }
    styled = build_sku_for(
        RETAILER, product, {"id": 1, "option1": "42", "price": "1",
                            "available": True, "barcode": None, "sku": "I032940_89"}
    )
    assert styled.style_code == "I032940_89"
    assert styled.barcode is None

    gtin = build_sku_for(
        RETAILER, product, {"id": 2, "option1": "42", "price": "1",
                            "available": True, "barcode": None, "sku": "4064958104131"}
    )
    assert gtin.style_code is None
    assert gtin.barcode == "4064958104131"


def test_build_sku_compound_sku_is_style_code_not_barcode():
    # Regression: a compound ``STYLE-SIZE`` SKU (e.g. Overkill) must be stored
    # as a style code, never misreported as an 8-digit GTIN barcode.
    product = {
        "handle": "carhartt-cap",
        "vendor": "Carhartt WIP",
        "title": "Cap",
        "options": [{"name": "Size"}],
    }
    variant = {"id": 3, "option1": "One Size", "price": "19.00",
               "available": True, "barcode": None, "sku": "114805-37"}
    sku = build_sku_for(RETAILER, product, variant)
    assert sku.barcode is None
    assert sku.style_code == "114805-37"


def test_build_sku_unknown_stock_returns_none():
    product = {"handle": "h", "vendor": "Veja", "title": "V-10", "options": []}
    variant = {"id": 1, "price": "1", "available": "yes", "barcode": None, "sku": None}
    assert build_sku_for(RETAILER, product, variant) is None


def test_build_sku_unresolved_brand_returns_none():
    product = {"handle": "h", "vendor": "Nike", "title": "Air Max", "options": []}
    variant = {"id": 1, "price": "1", "available": True, "barcode": None, "sku": None}
    assert build_sku_for(RETAILER, product, variant) is None


def test_build_sku_honours_brand_hint():
    product = {"handle": "h", "vendor": "", "title": "Mystery", "options": []}
    variant = {"id": 1, "price": "1", "available": False, "barcode": None, "sku": None}
    sku = build_sku_for(RETAILER, product, variant, brand="Carhartt WIP")
    assert sku.brand == "Carhartt WIP"
    assert sku.in_stock is False


def test_parse_products_counts_skipped():
    products = [
        ({"handle": "a", "vendor": "Veja", "title": "V-10", "options": [], "variants": [
            {"id": 1, "price": "1", "available": True, "barcode": None, "sku": None},
            {"id": 2, "price": "1", "available": None, "barcode": None, "sku": None},
        ]}, "Veja"),
    ]
    skus, skipped = parse_products_for(RETAILER, products)
    assert len(skus) == 1
    assert skipped == 1


# ---------------------------------------------------------------------------
# Per-shop configuration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", SHOP_MODULES)
def test_shop_config(module):
    shop_id, currency = EXPECTED_SHOPS[module]
    assert module.SHOP_ID == shop_id
    assert module.RETAILER.shop_id == shop_id
    assert module.RETAILER.currency == currency
    assert module.RETAILER.base_url.startswith("https://")


def test_nugnes_full_scan_only():
    assert nugnes1920.RETAILER.use_collections is False
    assert nugnes1920.RETAILER.full_scan is True


def test_sneakersnstuff_full_scan_enabled():
    assert sneakersnstuff.RETAILER.full_scan is True


def test_biffi_decimal_comma_enabled():
    assert biffi.RETAILER.decimal_comma is True
    assert not caliroots.RETAILER.decimal_comma


def test_biffi_uses_strict_discovery_and_slow_cadence():
    assert biffi.RETAILER.strict_discovery is True
    assert biffi.RETAILER.request_delay_s >= 60.0


def test_other_shops_keep_default_discovery_behaviour():
    for module in (sneakersnstuff, footway, xtreme, caliroots, beamhill, afew,
                   nugnes1920, overkillshop):
        assert module.RETAILER.strict_discovery is False
        assert module.RETAILER.request_delay_s == core.PAGE_DELAY_S


def test_decimal_comma_size_is_localized_for_flagged_retailer():
    product = {"handle": "h", "vendor": "Salomon", "title": "XT-6",
               "options": [{"name": "Taglia"}]}
    variant = {"id": 1, "option1": "7,5", "price": "1", "available": True,
               "barcode": None, "sku": "L49211000ALLOYBLACK-7,5"}
    comma = ShopifyRetailer(shop_id="biffi", base_url="https://www.biffi.com",
                            currency="EUR", decimal_comma=True)
    assert build_sku_for(comma, product, variant).size == "7.5"

    default = ShopifyRetailer(
        shop_id="other", base_url="https://other.test", currency="EUR"
    )
    assert build_sku_for(default, product, variant).size == "7,5"


# ---------------------------------------------------------------------------
# Rate limiting / collection discovery
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self.requests.append(url)
        return self._handler(url)


def _no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(core.asyncio, "sleep", fake_sleep)
    return sleeps


ALL_COLLECTIONS = {
    "carhartt-wip": ("Carhartt WIP", "carhartt"),
    "salomon": ("Salomon", "salomon"),
    "veja": ("Veja", "veja"),
}


def _collection_handler(catalogue, counters=None):
    def handler(url):
        for handle, (brand, slug) in catalogue.items():
            if f"/collections/{handle}/" in url:
                if counters is not None:
                    counters[handle] = counters.get(handle, 0) + 1
                product = {"handle": slug, "vendor": brand, "title": slug,
                           "options": [], "variants": []}
                return _FakeResponse(200, {"products": [product]})
        return _FakeResponse(404)

    return handler


class TestRetryAfterParsing:
    def test_seconds(self):
        assert core._retry_after_seconds("30") == 30.0

    def test_capped(self):
        assert core._retry_after_seconds("9999") == core.MAX_RETRY_AFTER_S

    def test_missing_and_invalid_fall_back(self):
        assert core._retry_after_seconds(None) == core.DEFAULT_RETRY_AFTER_S
        assert core._retry_after_seconds("not-a-date") == core.DEFAULT_RETRY_AFTER_S

    def test_http_date(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=30)
        header = format_datetime(when)
        assert 20.0 <= core._retry_after_seconds(header) <= 40.0


class TestGetJsonRateLimit:
    @pytest.mark.asyncio
    async def test_honours_retry_after_then_succeeds(self, monkeypatch):
        sleeps = _no_sleep(monkeypatch)
        responses = iter([
            _FakeResponse(429, headers={"Retry-After": "7"}),
            _FakeResponse(200, {"products": [{"handle": "x"}]}),
        ])
        client = _FakeClient(lambda url: next(responses))
        result = await core._get_json(
            client, "https://shop.test/collections/salomon/products.json"
        )
        assert result == {"products": [{"handle": "x"}]}
        assert sleeps == [7.0]

    @pytest.mark.asyncio
    async def test_raises_when_rate_limit_persists(self, monkeypatch):
        _no_sleep(monkeypatch)
        monkeypatch.setattr(core, "RATE_LIMIT_RETRIES", 2)
        client = _FakeClient(
            lambda url: _FakeResponse(429, headers={"Retry-After": "0"})
        )
        with pytest.raises(core.RateLimitedError):
            await core._get_json(
                client, "https://shop.test/collections/salomon/products.json"
            )

    @pytest.mark.asyncio
    async def test_missing_collection_returns_none(self, monkeypatch):
        _no_sleep(monkeypatch)
        client = _FakeClient(lambda url: _FakeResponse(404))
        assert await core._get_json(
            client, "https://shop.test/collections/nope/products.json"
        ) is None


class TestCollectionDiscovery:
    @pytest.mark.asyncio
    async def test_all_brand_collections_discovered(self, monkeypatch):
        _no_sleep(monkeypatch)
        monkeypatch.setattr(
            core, "_client", lambda: _FakeClient(_collection_handler(ALL_COLLECTIONS))
        )
        products = await core.fetch_products_for(RETAILER)
        assert {brand for _, brand in products} == {"Carhartt WIP", "Salomon", "Veja"}

    @pytest.mark.asyncio
    async def test_throttled_collection_is_retried_not_dropped(self, monkeypatch):
        _no_sleep(monkeypatch)
        counters = {}

        def handler(url):
            for handle, (brand, slug) in ALL_COLLECTIONS.items():
                if f"/collections/{handle}/" in url:
                    counters[handle] = counters.get(handle, 0) + 1
                    if handle == "salomon" and counters[handle] == 1:
                        return _FakeResponse(429, headers={"Retry-After": "1"})
                    product = {"handle": slug, "vendor": brand, "title": slug,
                               "options": [], "variants": []}
                    return _FakeResponse(200, {"products": [product]})
            return _FakeResponse(404)

        monkeypatch.setattr(core, "_client", lambda: _FakeClient(handler))
        products = await core.fetch_products_for(RETAILER)
        assert counters["salomon"] == 2
        assert {brand for _, brand in products} == {"Carhartt WIP", "Salomon", "Veja"}

    @pytest.mark.asyncio
    async def test_strict_discovery_aborts_on_persistent_throttle(self, monkeypatch):
        _no_sleep(monkeypatch)
        monkeypatch.setattr(core, "RATE_LIMIT_RETRIES", 2)
        strict = ShopifyRetailer(
            shop_id="biffi", base_url="https://www.biffi.com",
            currency="EUR", strict_discovery=True,
        )

        def handler(url):
            if "/collections/carhartt-wip/" in url:
                return _FakeResponse(200, {"products": []})
            return _FakeResponse(429, headers={"Retry-After": "0"})

        monkeypatch.setattr(core, "_client", lambda: _FakeClient(handler))
        with pytest.raises(core.RateLimitedError):
            await core.fetch_products_for(biffi.RETAILER)

    @pytest.mark.asyncio
    async def test_non_strict_discovery_skips_throttled_collection(self, monkeypatch):
        _no_sleep(monkeypatch)
        monkeypatch.setattr(core, "RATE_LIMIT_RETRIES", 2)

        def handler(url):
            for handle, (brand, slug) in ALL_COLLECTIONS.items():
                if f"/collections/{handle}/" in url:
                    if handle == "veja":
                        return _FakeResponse(429, headers={"Retry-After": "0"})
                    product = {"handle": slug, "vendor": brand, "title": slug,
                               "options": [], "variants": []}
                    return _FakeResponse(200, {"products": [product]})
            return _FakeResponse(404)

        monkeypatch.setattr(core, "_client", lambda: _FakeClient(handler))
        products = await core.fetch_products_for(RETAILER)
        assert [brand for _, brand in products] == ["Carhartt WIP", "Salomon"]


# ---------------------------------------------------------------------------
# Bounded per-product concurrency (run_scraper_for)
# ---------------------------------------------------------------------------

def _conc_product(handle, variants=None, vendor="Carhartt WIP"):
    return {
        "handle": handle,
        "vendor": vendor,
        "title": handle,
        "options": [{"name": "Size"}],
        "variants": variants
        if variants is not None
        else [{"id": 1, "option1": "M", "price": "100.00",
               "available": True, "barcode": None, "sku": None}],
    }


def _prepare_scrape(monkeypatch, products, *, fail_handle=None):
    """Patch catalogue fetch + DB so ``run_scraper_for`` runs fully offline."""
    state = {"live": 0, "peak": 0, "upserted": [], "captured": []}

    async def fake_fetch(retailer):
        return [(p, "Carhartt WIP") for p in products]

    async def fake_upsert(skus):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        try:
            await asyncio.sleep(0.01)
            state["captured"].extend(skus)
            if fail_handle is not None and skus and skus[0].title == fail_handle:
                raise RuntimeError("db down")
            state["upserted"].append(len(skus))
            return (len(skus), 0, 0)
        finally:
            state["live"] -= 1

    async def fake_counts(retailer):
        return {"db_total_skus": 0, "db_unique_urls": 0,
                "db_in_stock": 0, "db_out_stock": 0}

    monkeypatch.setattr(core, "fetch_products_for", fake_fetch)
    monkeypatch.setattr(core, "upsert_skus", fake_upsert)
    monkeypatch.setattr(core, "db_counts_for", fake_counts)
    return state


@pytest.mark.asyncio
async def test_run_scraper_for_bounds_product_concurrency(monkeypatch):
    products = [_conc_product(f"p{i}") for i in range(12)]
    state = _prepare_scrape(monkeypatch, products)

    stats = await core.run_scraper_for(RETAILER, concurrency=3)

    assert state["peak"] == 3
    assert stats["products_discovered"] == 12
    assert stats["successful"] == 12
    assert stats["products_processed"] == 12
    assert stats["failed"] == 0
    assert stats["skus_inserted"] == 12


@pytest.mark.asyncio
async def test_run_scraper_for_defaults_to_sequential(monkeypatch):
    products = [_conc_product(f"p{i}") for i in range(5)]
    state = _prepare_scrape(monkeypatch, products)

    stats = await core.run_scraper_for(RETAILER)  # default concurrency=1

    assert state["peak"] == 1
    assert stats["successful"] == 5


@pytest.mark.asyncio
async def test_run_scraper_for_respects_shared_global_gate(monkeypatch):
    products = [_conc_product(f"p{i}") for i in range(12)]
    state = _prepare_scrape(monkeypatch, products)
    gate = RefreshConcurrency(global_limit=2)

    stats = await core.run_scraper_for(RETAILER, concurrency=5, gate=gate)

    assert state["peak"] <= 2
    assert gate.peak <= 2
    assert gate.active == 0
    assert stats["successful"] == 12


@pytest.mark.asyncio
async def test_run_scraper_for_one_failure_does_not_cancel_others(monkeypatch):
    products = [_conc_product(f"p{i}") for i in range(6)]
    state = _prepare_scrape(monkeypatch, products, fail_handle="p3")

    stats = await core.run_scraper_for(RETAILER, concurrency=3)

    assert stats["successful"] == 5
    assert stats["failed"] == 1
    # The failed product's variant is still counted as seen.
    assert stats["variants_total"] == 6
    assert len(state["upserted"]) == 5


@pytest.mark.asyncio
async def test_run_scraper_for_preserves_mixed_per_size_stock(monkeypatch):
    product = _conc_product("p", variants=[
        {"id": 1, "option1": "S", "price": "10", "available": True,
         "barcode": "1111111111116", "sku": None},
        {"id": 2, "option1": "M", "price": "10", "available": False,
         "barcode": None, "sku": None},
    ])
    state = _prepare_scrape(monkeypatch, [product])

    await core.run_scraper_for(RETAILER, concurrency=3)

    by_size = {sku.size: sku for sku in state["captured"]}
    assert by_size["S"].in_stock is True
    assert by_size["M"].in_stock is False
