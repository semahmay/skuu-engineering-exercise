import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.scrapers.carhartt import (
    _extract_product_data,
    ProductData,
    SizeVariant,
    get_size_variants,
    scrape_product_page,
    save_skus,
    sku_exists,
)
from app.models.sku import SKU


SAMPLE_JSON_LD = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Test Jacket",
    "image": ["https://example.com/img1.jpg"],
    "sku": "TEST123_45",
    "size": ["XS", "S", "M", "L", "XL"],
    "material": "100% Cotton",
    "color": "Black",
    "brand": {"@type": "Brand", "name": "Carhartt WIP"},
    "offers": {
        "@type": "Offer",
        "price": 199,
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
        "url": "https://www.carhartt-wip.com/en-de/p/test-jacket-black-123",
        "itemCondition": "https://schema.org/NewCondition",
    },
}


def test_extract_product_data():
    product = _extract_product_data(SAMPLE_JSON_LD)

    assert isinstance(product, ProductData)
    assert product.title == "Test Jacket"
    assert product.color == "Black"
    assert product.price == 199.0
    assert product.currency == "EUR"
    assert product.barcode == "TEST123_45"
    assert product.url == "https://www.carhartt-wip.com/en-de/p/test-jacket-black-123"
    assert product.material == "100% Cotton"
    assert product.image_urls == ["https://example.com/img1.jpg"]


def test_extract_product_data_single_size():
    data = json.loads(json.dumps(SAMPLE_JSON_LD))
    data["size"] = ["0"]
    product = _extract_product_data(data)
    assert product.title == "Test Jacket"


def test_extract_product_data_no_size():
    data = json.loads(json.dumps(SAMPLE_JSON_LD))
    data.pop("size")
    product = _extract_product_data(data)
    assert product.title == "Test Jacket"


def test_extract_product_data_string_size():
    data = json.loads(json.dumps(SAMPLE_JSON_LD))
    data["size"] = "M"
    product = _extract_product_data(data)
    assert product.title == "Test Jacket"


# ---------------------------------------------------------------------------
# Fake DOM helpers for get_size_variants
# ---------------------------------------------------------------------------

class FakeLocator:
    """Minimal Playwright locator stand-in for get_size_variants."""

    def __init__(self, attrs=None, disabled=False, label_class="", xpath_children=None):
        self._attrs = attrs or {}
        self._disabled = disabled
        self._label_class = label_class
        self._xpath_children = xpath_children or []

    async def all(self):
        return self._children if hasattr(self, "_children") else []

    async def count(self):
        return len(self._xpath_children)

    async def get_attribute(self, name):
        return self._attrs.get(name)

    async def is_disabled(self):
        return self._disabled

    def locator(self, selector):
        if selector == "xpath=../..":
            parent = FakeLocator(attrs={"class": self._label_class})
            parent._children = self._xpath_children
            return parent
        raise ValueError(f"unsupported selector: {selector}")


class FakePage:
    def __init__(self, radios=None, json_ld_scripts=None):
        self._radios = radios or []
        self._json_ld_scripts = json_ld_scripts or []

    def locator(self, selector):
        if selector in ('input[name="intlSizes"]', 'input[name="size"]'):
            matches = [r for r in self._radios if r["name"] == selector.split('"')[1]]
            loc = FakeLocator()
            loc._children = [
                FakeRadio(r["attrs"], r.get("disabled", False), r.get("label_class", ""))
                for r in matches
            ]
            # .all() should return the radio objects themselves
            loc.all = lambda: _coro(loc._children)
            return loc
        if selector == 'script[type="application/ld+json"]':
            loc = FakeLocator()
            scripts = [
                FakeScript(json.dumps(data)) for data in self._json_ld_scripts
            ]
            loc.all = lambda: _coro(scripts)
            return loc
        raise ValueError(f"unsupported selector: {selector}")


def _coro(value):
    async def _inner():
        return value
    return _inner()


class FakeRadio:
    def __init__(self, attrs, disabled, label_class):
        self._attrs = attrs
        self._disabled = disabled
        self._label_class = label_class
        self._label = FakeLocator(attrs={"class": label_class})
        self._label._xpath_children = [self]

    async def get_attribute(self, name):
        return self._attrs.get(name)

    async def is_disabled(self):
        return self._disabled

    def locator(self, selector):
        if selector == "xpath=../..":
            return self._label
        raise ValueError(selector)


class FakeScript:
    def __init__(self, content):
        self._content = content

    async def inner_text(self):
        return self._content


# ---------------------------------------------------------------------------
# get_size_variants
# ---------------------------------------------------------------------------

def _radio(name, value, aria=None, radio_id=None, disabled=False, label_class=""):
    attrs = {"value": value}
    if aria is not None:
        attrs["aria-label"] = aria
    if radio_id is not None:
        attrs["id"] = radio_id
    return {"name": name, "attrs": attrs, "disabled": disabled, "label_class": label_class}


@pytest.mark.asyncio
async def test_size_variants_mixed_stock():
    """Available and unavailable sizes in one product."""
    page = FakePage(radios=[
        _radio("intlSizes", "XS", aria="XS", radio_id="v1", disabled=True,
               label_class="line-through text-midGrey pointer-events-none"),
        _radio("intlSizes", "S", aria="S", radio_id="v2", disabled=False,
               label_class="cursor-pointer"),
        _radio("intlSizes", "M", aria="M", radio_id="v3", disabled=False,
               label_class="cursor-pointer"),
    ])
    variants = await get_size_variants(page)
    assert len(variants) == 3
    by_size = {v.size: v for v in variants}
    assert by_size["XS"].in_stock is False
    assert by_size["S"].in_stock is True
    assert by_size["M"].in_stock is True


@pytest.mark.asyncio
async def test_size_variants_all_available():
    page = FakePage(radios=[
        _radio("intlSizes", s, aria=s, radio_id=f"v{i}", disabled=False)
        for i, s in enumerate(["XS", "S", "M", "L", "XL", "XXL"])
    ])
    variants = await get_size_variants(page)
    assert len(variants) == 6
    assert all(v.in_stock for v in variants)


@pytest.mark.asyncio
async def test_size_variants_one_size_available():
    """One Size product: input[name=size], value=0, no id, no aria-label."""
    page = FakePage(radios=[
        _radio("size", "0", radio_id=None, aria=None, disabled=False),
    ])
    variants = await get_size_variants(page)
    assert len(variants) == 1
    assert variants[0].size == "0"
    assert variants[0].in_stock is True


@pytest.mark.asyncio
async def test_size_variants_one_size_sold_out():
    page = FakePage(radios=[
        _radio("size", "0", disabled=True,
               label_class="line-through text-midGrey pointer-events-none"),
    ])
    variants = await get_size_variants(page)
    assert len(variants) == 1
    assert variants[0].in_stock is False


@pytest.mark.asyncio
async def test_size_variants_no_selector_returns_empty():
    page = FakePage(radios=[])
    variants = await get_size_variants(page)
    assert variants == []


@pytest.mark.asyncio
async def test_size_variants_label_class_without_disabled():
    """Sold-out detected via label classes even if disabled attr missing."""
    page = FakePage(radios=[
        _radio("intlSizes", "S", aria="S", radio_id="v1", disabled=False,
               label_class="line-through text-midGrey pointer-events-none"),
    ])
    variants = await get_size_variants(page)
    assert variants[0].in_stock is False


# ---------------------------------------------------------------------------
# scrape_product_page
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200):
        self.status = status


class ScrapePage:
    """Page double for scrape_product_page."""

    def __init__(self, status=200, json_ld=None, radios=None):
        self._status = status
        self._json_ld = json_ld
        self._radios = radios or []

    async def goto(self, url, **kwargs):
        return FakeResponse(self._status)

    async def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        return FakePage(radios=self._radios, json_ld_scripts=self._json_ld).locator(selector)


def _json_ld(sizes, availability="https://schema.org/InStock", name="Test Jacket", price=199):
    return [{
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "sku": "SKU1",
        "size": sizes,
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": "EUR",
            "availability": availability,
            "url": "https://example.com/p/1",
        },
    }]


@pytest.mark.asyncio
async def test_scrape_mixed_stock():
    page = ScrapePage(
        json_ld=_json_ld(["XS", "S", "M"]),
        radios=[
            _radio("intlSizes", "XS", aria="XS", radio_id="v1", disabled=True,
                   label_class="line-through text-midGrey pointer-events-none"),
            _radio("intlSizes", "S", aria="S", radio_id="v2"),
            _radio("intlSizes", "M", aria="M", radio_id="v3"),
        ],
    )
    with patch("app.scrapers.carhartt.accept_cookies", new=AsyncMock()):
        skus = await scrape_product_page(page, "https://example.com/p/1")

    assert len(skus) == 3
    by_size = {s.size: s for s in skus}
    assert by_size["XS"].in_stock is False
    assert by_size["S"].in_stock is True
    assert by_size["M"].in_stock is True
    assert all(s.shop == "carhartt_wip" for s in skus)
    assert all(s.url == "https://example.com/p/1" for s in skus)


@pytest.mark.asyncio
async def test_scrape_fully_sold_out_no_selector():
    """No size selector rendered -> JSON-LD sizes with in_stock=False."""
    page = ScrapePage(json_ld=_json_ld(["XS", "S", "M"]), radios=[])
    with patch("app.scrapers.carhartt.accept_cookies", new=AsyncMock()):
        skus = await scrape_product_page(page, "https://example.com/p/1")

    assert len(skus) == 3
    assert all(s.in_stock is False for s in skus)
    assert {s.size for s in skus} == {"XS", "S", "M"}


@pytest.mark.asyncio
async def test_scrape_one_size_display():
    """Numeric size '0' is displayed as 'One Size'."""
    page = ScrapePage(
        json_ld=_json_ld(["0"]),
        radios=[_radio("size", "0", disabled=False)],
    )
    with patch("app.scrapers.carhartt.accept_cookies", new=AsyncMock()):
        skus = await scrape_product_page(page, "https://example.com/p/1")

    assert len(skus) == 1
    assert skus[0].size == "One Size"
    assert skus[0].in_stock is True


@pytest.mark.asyncio
async def test_scrape_non_200_returns_empty():
    page = ScrapePage(status=404)
    with patch("app.scrapers.carhartt.accept_cookies", new=AsyncMock()):
        skus = await scrape_product_page(page, "https://example.com/404")
    assert skus == []


# ---------------------------------------------------------------------------
# Duplicate prevention: sku_exists / save_skus
# ---------------------------------------------------------------------------

def _make_sku(size="M", url="https://example.com/p/1"):
    return SKU(
        shop="carhartt_wip",
        brand="Carhartt WIP",
        title="Test Jacket",
        colour="Black",
        size=size,
        price=199.0,
        currency="EUR",
        barcode="SKU1",
        in_stock=True,
        url=url,
    )


@pytest.mark.asyncio
async def test_sku_exists_true_when_doc_present():
    collection = AsyncMock()
    collection.find_one = AsyncMock(return_value={"shop": "carhartt_wip"})
    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        assert await sku_exists("carhartt_wip", "https://example.com/p/1", "M") is True


@pytest.mark.asyncio
async def test_sku_exists_false_when_missing():
    collection = AsyncMock()
    collection.find_one = AsyncMock(return_value=None)
    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        assert await sku_exists("carhartt_wip", "https://example.com/p/1", "M") is False


@pytest.mark.asyncio
async def test_save_skus_skips_duplicates():
    """Second run inserts 0, skips all."""
    skus = [_make_sku(size=s) for s in ["XS", "S", "M"]]

    # First run: nothing exists
    with patch("app.scrapers.carhartt.sku_exists", new=AsyncMock(return_value=False)), \
         patch("app.scrapers.carhartt.insert_sku", new=AsyncMock()):
        ins1, skp1 = await save_skus(skus)
    assert ins1 == 3
    assert skp1 == 0

    # Second run: all exist
    with patch("app.scrapers.carhartt.sku_exists", new=AsyncMock(return_value=True)), \
         patch("app.scrapers.carhartt.insert_sku", new=AsyncMock()) as insert_mock:
        ins2, skp2 = await save_skus(skus)
    assert ins2 == 0
    assert skp2 == 3
    insert_mock.assert_not_called()


@pytest.mark.asyncio
async def test_save_skus_partially_existing():
    """Identity is shop+url+size: only missing sizes inserted."""
    skus = [_make_sku(size=s) for s in ["XS", "S", "M"]]

    async def exists(shop, url, size):
        return size == "S"

    inserted_sizes = []

    async def insert(sku):
        inserted_sizes.append(sku.size)

    with patch("app.scrapers.carhartt.sku_exists", new=exists), \
         patch("app.scrapers.carhartt.insert_sku", new=insert):
        ins, skp = await save_skus(skus)

    assert ins == 2
    assert skp == 1
    assert sorted(inserted_sizes) == ["M", "XS"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
