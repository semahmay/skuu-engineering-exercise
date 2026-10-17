"""Tests for the Salomon scraper (Shopify /products.json source).

No network calls: field extraction, per-variant stock mapping, URL uniqueness
for multi-colour products, pagination and the reused (shop, url, size) upsert
identity are all exercised with in-memory fixtures.
"""

import asyncio
from datetime import datetime

import pytest
from pymongo.errors import DuplicateKeyError

from app.refresh.concurrency import RefreshConcurrency
from app.scrapers import salomon
from app.scrapers.carhartt import upsert_skus
from app.scrapers.salomon import (
    BRAND,
    SHOP_ID,
    STORES,
    build_sku,
    extract_colour,
    extract_size,
    find_option_index,
    parse_products,
    variant_url,
)


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def make_product(handle, title, options, variants, product_type="Sneakers"):
    return {
        "handle": handle,
        "title": title,
        "product_type": product_type,
        "options": [
            {"name": name, "position": i + 1, "values": []}
            for i, name in enumerate(options)
        ],
        "variants": variants,
    }


def make_variant(vid, option1=None, option2=None, option3=None,
                 price="100.00", available=True, sku="STYLE-1", barcode=None):
    return {
        "id": vid,
        "option1": option1,
        "option2": option2,
        "option3": option3,
        "price": price,
        "available": available,
        "sku": sku,
        "barcode": barcode,
    }


# ---------------------------------------------------------------------------
# Option discovery
# ---------------------------------------------------------------------------

def test_find_option_index_colour_and_size():
    product = make_product("h", "T", ["Color", "Size"], [])
    assert find_option_index(product, salomon.COLOUR_OPTION_NAMES) == 0
    assert find_option_index(product, salomon.SIZE_OPTION_NAMES) == 1


def test_find_option_index_handles_case_and_alternate_names():
    product = make_product("h", "T", ["Colour", "Shoe size"], [])
    assert find_option_index(product, salomon.COLOUR_OPTION_NAMES) == 0
    assert find_option_index(product, salomon.SIZE_OPTION_NAMES) == 1


def test_find_option_index_absent_returns_none():
    product = make_product("h", "T", ["Model"], [])
    assert find_option_index(product, salomon.SIZE_OPTION_NAMES) is None
    assert find_option_index(product, salomon.COLOUR_OPTION_NAMES) is None


# ---------------------------------------------------------------------------
# Field extraction
# ---------------------------------------------------------------------------

def test_extract_size_and_colour_standard_order():
    product = make_product("h", "T", ["Color", "Size"], [])
    variant = make_variant(1, option1="Black", option2="42")
    assert extract_colour(product, variant) == "Black"
    assert extract_size(product, variant) == "42"


def test_extract_size_and_colour_reversed_order():
    product = make_product("h", "T", ["Size", "Colour"], [])
    variant = make_variant(1, option1="L", option2="White")
    assert extract_size(product, variant) == "L"
    assert extract_colour(product, variant) == "White"


def test_extract_size_defaults_to_one_size_without_size_option():
    product = make_product("h", "T", ["Color"], [])
    variant = make_variant(1, option1="Red")
    assert extract_size(product, variant) == "One Size"
    assert extract_colour(product, variant) == "Red"


def test_extract_colour_empty_when_no_colour_option():
    product = make_product("h", "T", ["Size"], [])
    variant = make_variant(1, option1="42")
    assert extract_colour(product, variant) == ""


# ---------------------------------------------------------------------------
# URL identity
# ---------------------------------------------------------------------------

def test_variant_url_includes_variant_id():
    store = STORES["au"]
    assert variant_url(store, "sense-ride", 111) == (
        "https://salomon.com.au/products/sense-ride?variant=111"
    )


def test_variant_url_without_variant_id():
    store = STORES["au"]
    assert variant_url(store, "sense-ride", None) == (
        "https://salomon.com.au/products/sense-ride"
    )


# ---------------------------------------------------------------------------
# SKU construction
# ---------------------------------------------------------------------------

def test_build_sku_maps_fields():
    product = make_product("sense-ride-gtx", "SENSE RIDE GTX", ["Color", "Size"], [])
    variant = make_variant(
        42, option1="Black", option2="42", price="159.99", available=True, barcode="123"
    )
    sku = build_sku(product, variant, STORES["au"])
    assert sku.shop == "salomon"
    assert sku.brand == "Salomon"
    assert sku.title == "SENSE RIDE GTX"
    assert sku.colour == "Black"
    assert sku.size == "42"
    assert sku.price == pytest.approx(159.99)
    assert sku.currency == "AUD"
    assert sku.barcode == "123"
    assert sku.in_stock is True
    assert sku.url == "https://salomon.com.au/products/sense-ride-gtx?variant=42"
    assert isinstance(sku.scraped_at, datetime)


@pytest.mark.parametrize("available,expected", [(True, True), (False, False)])
def test_build_sku_stock_reflects_variant_availability(available, expected):
    product = make_product("h", "T", ["Size"], [])
    variant = make_variant(1, option1="42", available=available)
    assert build_sku(product, variant, STORES["au"]).in_stock is expected


@pytest.mark.parametrize("bad", [None, 1, 0, "true", ""])
def test_build_sku_unknown_stock_is_skipped_not_guessed(bad):
    product = make_product("h", "T", ["Size"], [])
    variant = make_variant(1, option1="42")
    variant["available"] = bad
    assert build_sku(product, variant, STORES["au"]) is None


def test_build_sku_missing_available_key_is_skipped():
    product = make_product("h", "T", ["Size"], [])
    variant = make_variant(1, option1="42")
    del variant["available"]
    assert build_sku(product, variant, STORES["au"]) is None


def test_build_sku_price_defaults_to_zero_when_invalid():
    product = make_product("h", "T", ["Size"], [])
    variant = make_variant(1, option1="42", price="")
    assert build_sku(product, variant, STORES["au"]).price == 0.0


def test_build_sku_skips_missing_handle():
    product = make_product("", "T", ["Size"], [])
    variant = make_variant(1, option1="42")
    assert build_sku(product, variant, STORES["au"]) is None


def test_build_sku_barcode_null_when_absent():
    product = make_product("h", "T", ["Size"], [])
    variant = make_variant(1, option1="42", barcode=None)
    assert build_sku(product, variant, STORES["au"]).barcode is None


def test_currency_differs_per_store():
    product = make_product("h", "T", ["Size"], [])
    variant = make_variant(1, option1="42")
    assert build_sku(product, variant, STORES["nz"]).currency == "NZD"


# ---------------------------------------------------------------------------
# Product parsing
# ---------------------------------------------------------------------------

def test_parse_products_multiple_colours_distinct_urls():
    product = make_product(
        "x",
        "T",
        ["Color", "Size"],
        [
            make_variant(1, option1="Black", option2="42"),
            make_variant(2, option1="White", option2="42"),
        ],
    )
    skus, skipped = parse_products([product], STORES["au"])
    assert skipped == 0
    assert len(skus) == 2
    assert {s.url for s in skus} == {
        "https://salomon.com.au/products/x?variant=1",
        "https://salomon.com.au/products/x?variant=2",
    }


def test_parse_products_counts_skipped_variants():
    product = make_product(
        "x",
        "T",
        ["Size"],
        [
            make_variant(1, option1="42"),
            make_variant(2, option1="43", available=None),
        ],
    )
    skus, skipped = parse_products([product], STORES["au"])
    assert len(skus) == 1
    assert skipped == 1


# ---------------------------------------------------------------------------
# Catalogue fetch (paginated)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_all_products_paginates_until_short_page(monkeypatch):
    monkeypatch.setattr(salomon, "PAGE_LIMIT", 2)
    pages = {
        1: {"products": [make_product("a", "A", ["Size"], []),
                         make_product("b", "B", ["Size"], [])]},
        2: {"products": [make_product("c", "C", ["Size"], []),
                         make_product("d", "D", ["Size"], [])]},
        3: {"products": [make_product("e", "E", ["Size"], [])]},
    }

    async def fake_get_json(client, url, retries=3):
        page = int(url.rsplit("page=", 1)[1])
        return pages.get(page, {"products": []})

    monkeypatch.setattr(salomon, "_get_json", fake_get_json)
    products = await salomon.fetch_all_products(STORES["au"], delay_ms=0)
    assert [p["handle"] for p in products] == ["a", "b", "c", "d", "e"]


@pytest.mark.asyncio
async def test_fetch_all_products_stops_at_limit(monkeypatch):
    monkeypatch.setattr(salomon, "PAGE_LIMIT", 2)

    async def fake_get_json(client, url, retries=3):
        return {"products": [make_product("x", "X", ["Size"], []),
                             make_product("y", "Y", ["Size"], [])]}

    monkeypatch.setattr(salomon, "_get_json", fake_get_json)
    products = await salomon.fetch_all_products(STORES["au"], limit=1, delay_ms=0)
    assert len(products) == 2


# ---------------------------------------------------------------------------
# Upsert / duplicate prevention (reused from Carhartt)
# ---------------------------------------------------------------------------

class _UpdateResult:
    """Minimal stand-in for pymongo's UpdateResult (upserted_id only)."""

    def __init__(self, upserted_id):
        self.upserted_id = upserted_id


class FakeCollection:
    """In-memory stand-in enforcing the unique (shop, url, size) identity."""

    def __init__(self):
        self.docs = {}
        self._next_id = 1

    async def find_one(self, query, projection=None):
        for doc in self.docs.values():
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None

    async def insert_one(self, doc):
        key = (doc["shop"], doc["url"], doc["size"])
        if key in self.docs:
            raise DuplicateKeyError("duplicate key")
        stored = dict(doc)
        stored["_id"] = self._next_id
        self._next_id += 1
        self.docs[key] = stored

    async def update_one(self, query, update, upsert=False):
        for doc in self.docs.values():
            if all(doc.get(k) == v for k, v in query.items()):
                doc.update(update.get("$set", {}))
                return _UpdateResult(None)
        if upsert:
            new = dict(query)
            new.update(update.get("$set", {}))
            new["_id"] = self._next_id
            self._next_id += 1
            self.docs[(new["shop"], new["url"], new["size"])] = new
            return _UpdateResult(new["_id"])
        return _UpdateResult(None)


@pytest.mark.asyncio
async def test_upsert_inserts_then_refreshes_without_duplicates(monkeypatch):
    fake = FakeCollection()

    async def fake_get_collection():
        return fake

    monkeypatch.setattr("app.scrapers.carhartt.get_sku_collection", fake_get_collection)

    product = make_product("x", "T", ["Size"], [])
    sku = build_sku(product, make_variant(1, option1="42", price="100"), STORES["au"])

    inserted, updated, failed = await upsert_skus([sku])
    assert (inserted, updated, failed) == (1, 0, 0)

    sku2 = build_sku(
        product, make_variant(1, option1="42", price="80", available=False), STORES["au"]
    )
    inserted, updated, failed = await upsert_skus([sku2])
    assert (inserted, updated, failed) == (0, 1, 0)
    assert len(fake.docs) == 1
    assert next(iter(fake.docs.values()))["in_stock"] is False


# ---------------------------------------------------------------------------
# Bounded per-product concurrency
# ---------------------------------------------------------------------------

def _salomon_products(n, sizes=4):
    return [
        make_product(
            f"sal-{i}",
            f"XT-6 {i}",
            ["Size"],
            [
                make_variant(i * 10 + j, option1=str(40 + j), available=(j % 2 == 0))
                for j in range(sizes)
            ],
        )
        for i in range(n)
    ]


def _patch_salomon_scrape(monkeypatch, products):
    state = {"live": 0, "peak": 0}

    async def fake_fetch(store, limit=None):
        return products

    async def fake_upsert(skus):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        try:
            await asyncio.sleep(0.01)
            return (len(skus), 0, 0)
        finally:
            state["live"] -= 1

    async def fake_counts():
        return {"db_total_skus": 0, "db_unique_urls": 0, "db_in_stock": 0, "db_out_stock": 0}

    monkeypatch.setattr(salomon, "fetch_all_products", fake_fetch)
    monkeypatch.setattr(salomon, "upsert_skus", fake_upsert)
    monkeypatch.setattr(salomon, "db_counts", fake_counts)
    return state


@pytest.mark.asyncio
async def test_run_scraper_bounds_product_concurrency(monkeypatch):
    state = _patch_salomon_scrape(monkeypatch, _salomon_products(12))

    stats = await salomon.run_scraper(concurrency=3)

    assert state["peak"] == 3
    assert stats["successful"] == 12
    assert stats["products_processed"] == 12
    assert stats["skus_inserted"] == 48


@pytest.mark.asyncio
async def test_run_scraper_defaults_to_sequential(monkeypatch):
    state = _patch_salomon_scrape(monkeypatch, _salomon_products(5))

    stats = await salomon.run_scraper()

    assert state["peak"] == 1
    assert stats["successful"] == 5


@pytest.mark.asyncio
async def test_run_scraper_respects_shared_global_gate(monkeypatch):
    state = _patch_salomon_scrape(monkeypatch, _salomon_products(10))
    gate = RefreshConcurrency(global_limit=2)

    stats = await salomon.run_scraper(concurrency=5, gate=gate)

    assert state["peak"] <= 2
    assert gate.active == 0
    assert stats["successful"] == 10
