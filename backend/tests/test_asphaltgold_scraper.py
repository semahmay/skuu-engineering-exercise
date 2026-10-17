"""Tests for the AsphaltGold retailer scraper (Shopify /products.json source).

No network calls: brand filtering, option-name discovery, colour/size
extraction, per-variant stock mapping, URL uniqueness across colours, pagination
and the reused (shop, url, size) upsert identity are all exercised in memory.
"""

import asyncio

import pytest
from pymongo.errors import DuplicateKeyError

from app.refresh.concurrency import RefreshConcurrency
from app.scrapers import asphaltgold
from app.scrapers.carhartt import upsert_skus


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def make_product(handle, title, options, variants, vendor="Salomon"):
    return {
        "handle": handle,
        "title": title,
        "vendor": vendor,
        "options": [{"name": n, "position": i + 1} for i, n in enumerate(options)],
        "variants": variants,
    }


def make_variant(vid, option1=None, option2=None, price="100.00", available=True, barcode=None):
    return {
        "id": vid,
        "title": "",
        "option1": option1,
        "option2": option2,
        "sku": "SKU-1",
        "price": price,
        "available": available,
        "barcode": barcode,
    }


# ---------------------------------------------------------------------------
# Brand filtering
# ---------------------------------------------------------------------------

def test_is_overlap_brand_accepts_our_three_brands():
    for brand in ("Salomon", "Veja", "Carhartt WIP"):
        assert asphaltgold.is_overlap_brand(make_product("h", "t", ["Size"], [], vendor=brand))


def test_is_overlap_brand_rejects_other_brands():
    assert not asphaltgold.is_overlap_brand(make_product("h", "t", ["Size"], [], vendor="Nike"))
    assert not asphaltgold.is_overlap_brand(make_product("h", "t", ["Size"], [], vendor=None))


# ---------------------------------------------------------------------------
# Option discovery (order-independent, name-based)
# ---------------------------------------------------------------------------

def test_find_option_index_size_first_order():
    product = make_product("h", "t", ["Size", "Color"], [])
    assert asphaltgold.find_option_index(product, asphaltgold.SIZE_OPTION_NAMES) == 0
    assert asphaltgold.find_option_index(product, asphaltgold.COLOUR_OPTION_NAMES) == 1


def test_find_option_index_colour_first_order():
    product = make_product("h", "t", ["Color", "Size"], [])
    assert asphaltgold.find_option_index(product, asphaltgold.COLOUR_OPTION_NAMES) == 0
    assert asphaltgold.find_option_index(product, asphaltgold.SIZE_OPTION_NAMES) == 1


def test_extract_size_and_colour_size_first():
    product = make_product("h", "t", ["Size", "Color"], [])
    variant = {"option1": "42 2/3", "option2": "Paloma / Cloudburst"}
    assert asphaltgold.extract_size(product, variant) == "42 2/3"
    assert asphaltgold.extract_colour(product, variant) == "Paloma / Cloudburst"


def test_extract_size_and_colour_colour_first():
    product = make_product("h", "t", ["Colour", "Shoe size"], [])
    variant = {"option1": "White", "option2": "L"}
    assert asphaltgold.extract_size(product, variant) == "L"
    assert asphaltgold.extract_colour(product, variant) == "White"


def test_extract_size_defaults_to_one_size_without_size_option():
    product = make_product("h", "t", ["Color"], [])
    assert asphaltgold.extract_size(product, {"option1": "Red"}) == "One Size"
    assert asphaltgold.extract_colour(product, {"option1": "Red"}) == "Red"


def test_extract_colour_empty_without_colour_option():
    product = make_product("h", "t", ["Size"], [])
    assert asphaltgold.extract_colour(product, {"option1": "42"}) == ""


# ---------------------------------------------------------------------------
# URL identity
# ---------------------------------------------------------------------------

def test_variant_url_includes_variant_id():
    assert asphaltgold.variant_url("xt-4-og", 111) == (
        "https://www.asphaltgold.com/products/xt-4-og?variant=111"
    )


def test_variant_url_without_variant_id():
    assert asphaltgold.variant_url("xt-4-og", None) == (
        "https://www.asphaltgold.com/products/xt-4-og"
    )


# ---------------------------------------------------------------------------
# SKU construction
# ---------------------------------------------------------------------------

def test_build_sku_maps_fields():
    product = make_product("xt-4-og", "Salomon XT-4 OG GTX", ["Size", "Color"], [], vendor="Salomon")
    variant = {"id": 9, "option1": "42", "option2": "Paloma / Cloudburst",
               "price": "209.00", "available": True, "barcode": None}
    sku = asphaltgold.build_sku(product, variant)
    assert sku.shop == "asphaltgold"
    assert sku.brand == "Salomon"
    assert sku.title == "Salomon XT-4 OG GTX"
    assert sku.colour == "Paloma / Cloudburst"
    assert sku.size == "42"
    assert sku.price == pytest.approx(209.00)
    assert sku.currency == "EUR"
    assert sku.barcode is None
    assert sku.in_stock is True
    assert sku.url == "https://www.asphaltgold.com/products/xt-4-og?variant=9"


def test_build_sku_reads_brand_from_product_vendor():
    product = make_product("detroit", "Carhartt WIP OG Detroit Jacket", ["Size", "Color"], [],
                           vendor="Carhartt WIP")
    sku = asphaltgold.build_sku(product, {"id": 1, "option1": "M", "option2": "Black",
                                          "price": "199", "available": False, "barcode": None})
    assert sku.brand == "Carhartt WIP"
    assert sku.in_stock is False


@pytest.mark.parametrize("available", [True, False])
def test_build_sku_respects_bool_stock(available):
    product = make_product("h", "t", ["Size", "Color"], [])
    sku = asphaltgold.build_sku(product, {"id": 1, "option1": "42", "option2": "Red",
                                          "price": "1", "available": available, "barcode": None})
    assert sku.in_stock is available


@pytest.mark.parametrize("bad", [None, "true", 1, 0, "yes"])
def test_build_sku_skips_non_bool_stock(bad):
    product = make_product("h", "t", ["Size", "Color"], [])
    sku = asphaltgold.build_sku(product, {"id": 1, "option1": "42", "option2": "Red",
                                          "price": "1", "available": bad, "barcode": None})
    assert sku is None


def test_build_sku_skips_missing_handle():
    product = make_product("", "t", ["Size", "Color"], [])
    sku = asphaltgold.build_sku(product, {"id": 1, "option1": "42", "option2": "Red",
                                          "price": "1", "available": True, "barcode": None})
    assert sku is None


def test_build_sku_drops_empty_barcode_to_none():
    product = make_product("h", "t", ["Size", "Color"], [])
    sku = asphaltgold.build_sku(product, {"id": 1, "option1": "42", "option2": "Red",
                                          "price": "1", "available": True, "barcode": ""})
    assert sku.barcode is None


def test_build_sku_bad_price_becomes_zero():
    product = make_product("h", "t", ["Size", "Color"], [])
    sku = asphaltgold.build_sku(product, {"id": 1, "option1": "42", "option2": "Red",
                                          "price": "n/a", "available": True, "barcode": None})
    assert sku.price == 0.0


# ---------------------------------------------------------------------------
# parse_products
# ---------------------------------------------------------------------------

def test_parse_products_unique_url_per_colour():
    product = make_product("x", "T", ["Size", "Color"], [])
    variants = [
        {"id": 1, "option1": "42", "option2": "Red", "price": "1", "available": True, "barcode": None},
        {"id": 2, "option1": "42", "option2": "Blue", "price": "1", "available": False, "barcode": None},
    ]
    skus, skipped = asphaltgold.parse_products([{**product, "variants": variants}])
    assert skipped == 0
    assert {s.url for s in skus} == {
        "https://www.asphaltgold.com/products/x?variant=1",
        "https://www.asphaltgold.com/products/x?variant=2",
    }


def test_parse_products_counts_skipped_variants():
    product = make_product("x", "T", ["Size", "Color"], [])
    variants = [
        {"id": 1, "option1": "42", "option2": "Red", "price": "1", "available": True, "barcode": None},
        {"id": 2, "option1": "43", "option2": "Red", "price": "1", "available": None, "barcode": None},
    ]
    skus, skipped = asphaltgold.parse_products([{**product, "variants": variants}])
    assert len(skus) == 1
    assert skipped == 1


# ---------------------------------------------------------------------------
# Pagination + brand filtering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_products_paginates_and_filters_brands(monkeypatch):
    monkeypatch.setattr(asphaltgold, "PAGE_LIMIT", 2)
    monkeypatch.setattr(asphaltgold, "PAGE_DELAY_S", 0)

    pages = {
        1: [{"handle": "a", "vendor": "Salomon", "variants": []},
            {"handle": "b", "vendor": "Nike", "variants": []}],
        2: [{"handle": "c", "vendor": "Veja", "variants": []},
            {"handle": "d", "vendor": "Adidas", "variants": []}],
        3: [{"handle": "e", "vendor": "Carhartt WIP", "variants": []}],
    }

    async def fake_get_json(client, url, retries=3):
        page = int(url.split("page=")[1])
        batch = pages.get(page, [])
        return {"products": batch}

    monkeypatch.setattr(asphaltgold, "_get_json", fake_get_json)
    products = await asphaltgold.fetch_products()
    assert [p["handle"] for p in products] == ["a", "c", "e"]


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

    product = make_product("x", "T", ["Size", "Color"], [], vendor="Salomon")
    sku = asphaltgold.build_sku(
        product, {"id": 1, "option1": "42", "option2": "Red", "price": "100",
                  "available": True, "barcode": None}
    )
    inserted, updated, failed = await upsert_skus([sku])
    assert (inserted, updated, failed) == (1, 0, 0)

    sku2 = asphaltgold.build_sku(
        product, {"id": 1, "option1": "42", "option2": "Red", "price": "100",
                  "available": False, "barcode": None}
    )
    inserted, updated, failed = await upsert_skus([sku2])
    assert (inserted, updated, failed) == (0, 1, 0)
    key = ("asphaltgold", "https://www.asphaltgold.com/products/x?variant=1", "42")
    assert fake.docs[key]["in_stock"] is False
    assert len(fake.docs) == 1


# ---------------------------------------------------------------------------
# Bounded per-product concurrency
# ---------------------------------------------------------------------------

def _asphaltgold_products(n, sizes=4):
    return [
        make_product(
            f"ag-{i}",
            f"Shoe {i}",
            ["Size"],
            [
                make_variant(i * 10 + j, option1=str(40 + j), available=(j % 2 == 0))
                for j in range(sizes)
            ],
            vendor="Salomon",
        )
        for i in range(n)
    ]


def _patch_asphaltgold_scrape(monkeypatch, products):
    state = {"live": 0, "peak": 0}

    async def fake_fetch():
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

    monkeypatch.setattr(asphaltgold, "fetch_products", fake_fetch)
    monkeypatch.setattr(asphaltgold, "upsert_skus", fake_upsert)
    monkeypatch.setattr(asphaltgold, "db_counts", fake_counts)
    return state


@pytest.mark.asyncio
async def test_run_scraper_bounds_product_concurrency(monkeypatch):
    state = _patch_asphaltgold_scrape(monkeypatch, _asphaltgold_products(12))

    stats = await asphaltgold.run_scraper(concurrency=3)

    assert state["peak"] == 3
    assert stats["successful"] == 12
    assert stats["products_processed"] == 12
    assert stats["skus_inserted"] == 48


@pytest.mark.asyncio
async def test_run_scraper_defaults_to_sequential(monkeypatch):
    state = _patch_asphaltgold_scrape(monkeypatch, _asphaltgold_products(5))

    stats = await asphaltgold.run_scraper()

    assert state["peak"] == 1
    assert stats["successful"] == 5


@pytest.mark.asyncio
async def test_run_scraper_respects_shared_global_gate(monkeypatch):
    state = _patch_asphaltgold_scrape(monkeypatch, _asphaltgold_products(10))
    gate = RefreshConcurrency(global_limit=2)

    stats = await asphaltgold.run_scraper(concurrency=5, gate=gate)

    assert state["peak"] <= 2
    assert gate.active == 0
    assert stats["successful"] == 10
