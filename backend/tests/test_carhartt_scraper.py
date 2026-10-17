import pytest
import json
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

import app.scrapers.carhartt as carhartt
from app.scrapers.carhartt import (
    _extract_product_data,
    _recover_scrape_page,
    _ScrapeStack,
    _close_page,
    ProductData,
    SizeVariant,
    SIZE_SELECTOR,
    MAX_CONCURRENT_PRODUCTS,
    MAX_CONCURRENCY_LIMIT,
    MAX_ATTEMPTS_PER_URL,
    block_heavy_resources,
    get_size_variants,
    run_scraper,
    scrape_attempt,
    scrape_product_page,
    save_skus,
    sku_exists,
    upsert_skus,
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
# Fake DOM helpers
# ---------------------------------------------------------------------------

def _rows_for(radios):
    """The exact rows the in-page size-extraction JS returns.

    Mirrors the production JS: prefer ``input[name="intlSizes"]`` and fall back
    to ``input[name="size"]`` only when there are no intlSizes radios.
    """
    multi = [r for r in radios if r["name"] == "intlSizes"]
    one = [r for r in radios if r["name"] == "size"]
    chosen = multi if multi else one
    return [
        {
            "value": r["attrs"].get("value"),
            "aria": r["attrs"].get("aria-label"),
            "id": r["attrs"].get("id"),
            "disabled": r.get("disabled", False),
            "label_class": r.get("label_class", ""),
        }
        for r in chosen
    ]


class FakeScript:
    def __init__(self, content):
        self._content = content

    async def inner_text(self):
        return self._content


class _ScriptLocator:
    def __init__(self, scripts):
        self._scripts = scripts

    async def all(self):
        return self._scripts


class FakePage:
    """Page double exposing only what the production code now calls.

    Size extraction goes through a single ``evaluate`` (no per-element locator
    calls), so ``locator`` only needs to serve the JSON-LD script selector.
    """

    def __init__(self, radios=None, json_ld_scripts=None, evaluate_raises=False):
        self._radios = radios or []
        self._json_ld_scripts = json_ld_scripts or []
        self._evaluate_raises = evaluate_raises
        self.evaluate_calls = 0

    async def evaluate(self, script):
        self.evaluate_calls += 1
        if self._evaluate_raises:
            raise RuntimeError("evaluate blew up")
        return _rows_for(self._radios)

    def locator(self, selector):
        if selector == 'script[type="application/ld+json"]':
            return _ScriptLocator(
                [FakeScript(json.dumps(d)) for d in self._json_ld_scripts]
            )
        raise ValueError(f"unsupported selector: {selector}")


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


@pytest.mark.asyncio
async def test_size_variants_uses_a_single_evaluate():
    """Extraction is one in-page pass, not one locator call per element.

    ``FakePage.locator`` raises for the size selectors, so this also proves the
    implementation no longer issues per-element locator calls.
    """
    page = FakePage(radios=[
        _radio("intlSizes", "M", aria="M", radio_id="v1"),
        _radio("intlSizes", "L", aria="L", radio_id="v2"),
    ])
    variants = await get_size_variants(page)
    assert page.evaluate_calls == 1
    assert [v.size for v in variants] == ["M", "L"]


@pytest.mark.asyncio
async def test_size_variants_prefers_intlsizes_over_size():
    """When both selectors exist, intlSizes wins (matches the old fallback)."""
    page = FakePage(radios=[
        _radio("intlSizes", "M", aria="M", radio_id="v1"),
        _radio("size", "0"),
    ])
    variants = await get_size_variants(page)
    assert [v.size for v in variants] == ["M"]


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
        self._page = FakePage(radios=self._radios, json_ld_scripts=self._json_ld)
        self.wait_for_selector_calls = []
        self.wait_for_timeout_calls = 0

    async def goto(self, url, **kwargs):
        return FakeResponse(self._status)

    async def wait_for_selector(self, selector, **kwargs):
        self.wait_for_selector_calls.append((selector, kwargs))
        return None

    async def wait_for_timeout(self, ms):
        self.wait_for_timeout_calls += 1

    async def evaluate(self, script):
        return await self._page.evaluate(script)

    def locator(self, selector):
        return self._page.locator(selector)


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
# Readiness wait, resource blocking, and retry recovery
# ---------------------------------------------------------------------------

class _NoSelectorPage(ScrapePage):
    """Page whose size selector never attaches (e.g. fully sold out)."""

    async def wait_for_selector(self, selector, **kwargs):
        raise TimeoutError("no size selector")


@pytest.mark.asyncio
async def test_scrape_attempt_waits_for_selector_instead_of_fixed_sleep():
    page = ScrapePage(
        json_ld=_json_ld(["S"]),
        radios=[_radio("intlSizes", "S", aria="S", radio_id="v1")],
    )
    with patch("app.scrapers.carhartt.accept_cookies", new=AsyncMock()):
        skus, reason = await scrape_attempt(page, "https://example.com/p/1")

    assert reason == ""
    assert [s.size for s in skus] == ["S"]
    assert page.wait_for_timeout_calls == 0  # no blind fixed sleep
    assert page.wait_for_selector_calls
    selector, kwargs = page.wait_for_selector_calls[0]
    assert selector == SIZE_SELECTOR
    assert kwargs.get("state") == "attached"


@pytest.mark.asyncio
async def test_scrape_attempt_tolerates_missing_size_selector():
    """If the selector never attaches we still fall back to JSON-LD sizes."""
    page = _NoSelectorPage(json_ld=_json_ld(["XS", "S"]), radios=[])
    with patch("app.scrapers.carhartt.accept_cookies", new=AsyncMock()):
        skus, reason = await scrape_attempt(page, "https://example.com/p/1")

    assert reason == ""
    assert {s.size for s in skus} == {"XS", "S"}
    assert all(s.in_stock is False for s in skus)


class _RouteRequest:
    def __init__(self, resource_type):
        self.resource_type = resource_type


class _Route:
    def __init__(self, resource_type, abort_exc=None, continue_exc=None):
        self.request = _RouteRequest(resource_type)
        self.abort_calls = 0
        self.continue_calls = 0
        self._abort_exc = abort_exc
        self._continue_exc = continue_exc

    async def abort(self):
        self.abort_calls += 1
        if self._abort_exc:
            raise self._abort_exc

    async def continue_(self):
        self.continue_calls += 1
        if self._continue_exc:
            raise self._continue_exc


class _RoutePage:
    def __init__(self):
        self.pattern = None
        self.handler = None

    async def route(self, pattern, handler):
        self.pattern = pattern
        self.handler = handler


@pytest.mark.asyncio
async def test_block_heavy_resources_blocks_and_continues():
    page = _RoutePage()
    await block_heavy_resources(page)
    assert page.pattern == "**/*"

    css = _Route("stylesheet")
    doc = _Route("document")
    await page.handler(css)
    await page.handler(doc)
    assert css.abort_calls == 1
    assert doc.continue_calls == 1


@pytest.mark.asyncio
async def test_block_heavy_resources_swallows_route_errors():
    """A stale interception must never raise out of the route handler."""
    page = _RoutePage()
    await block_heavy_resources(page)

    image = _Route("image", abort_exc=RuntimeError("Invalid InterceptionId"))
    script = _Route("script", continue_exc=RuntimeError("Invalid InterceptionId"))
    await page.handler(image)   # must not raise
    await page.handler(script)  # must not raise
    assert image.abort_calls == 1
    assert script.continue_calls == 1


class _RecoveryPage:
    def __init__(self, closed=False):
        self._closed = closed
        self.close_calls = 0
        self.default_timeout = None

    def is_closed(self):
        return self._closed

    async def close(self):
        self.close_calls += 1
        self._closed = True

    def set_default_timeout(self, ms):
        self.default_timeout = ms


class _RecoveryContext:
    def __init__(self, fail=False):
        self.fail = fail
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        if self.fail:
            raise RuntimeError("context is dead")
        return _RecoveryPage()


@pytest.mark.asyncio
async def test_recover_scrape_page_reuses_browser_and_context():
    context = _RecoveryContext()
    old_page = _RecoveryPage()
    stack = ("playwright", "browser", context, old_page)

    with patch("app.scrapers.carhartt.block_heavy_resources", new=AsyncMock()):
        new_stack = await _recover_scrape_page(stack)

    assert new_stack[0] == "playwright"
    assert new_stack[1] == "browser"
    assert new_stack[2] is context
    assert isinstance(new_stack[3], _RecoveryPage)
    assert new_stack[3] is not old_page
    assert old_page.close_calls == 1
    assert context.new_page_calls == 1


@pytest.mark.asyncio
async def test_recover_scrape_page_falls_back_to_full_rebuild():
    context = _RecoveryContext(fail=True)
    old_page = _RecoveryPage()
    stack = ("playwright", "browser", context, old_page)
    rebuilt = ("pw2", "b2", "c2", "p2")

    with patch(
        "app.scrapers.carhartt._recreate_scrape_page",
        new=AsyncMock(return_value=rebuilt),
    ) as recreate:
        result = await _recover_scrape_page(stack)

    assert result == rebuilt
    recreate.assert_awaited_once()
    assert context.new_page_calls == 1


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


# ---------------------------------------------------------------------------
# upsert_skus: single conditional upsert on (shop, url, size)
# ---------------------------------------------------------------------------


class _UpdateResult:
    """Minimal stand-in for pymongo's UpdateResult (upserted_id only)."""

    def __init__(self, upserted_id=None):
        self.upserted_id = upserted_id


class _UpsertCollection:
    """In-memory collection recording the exact calls ``upsert_skus`` makes.

    ``update_side_effects`` is a list of exceptions (or ``None``) consumed one
    per ``update_one`` call, so races and per-SKU failures can be simulated.
    """

    def __init__(self, docs=None, update_side_effects=None):
        self.docs = {k: dict(v) for k, v in (docs or {}).items()}
        self.find_one_calls = 0
        self.insert_one_calls = 0
        self.update_one_calls = []  # (query, upsert_flag)
        self._side_effects = list(update_side_effects or [])

    async def find_one(self, query, projection=None):
        self.find_one_calls += 1
        for doc in self.docs.values():
            if all(doc.get(k) == v for k, v in query.items()):
                return doc
        return None

    async def insert_one(self, doc):
        self.insert_one_calls += 1
        key = (doc["shop"], doc["url"], doc["size"])
        if key in self.docs:
            raise DuplicateKeyError("duplicate key")
        stored = dict(doc)
        stored["_id"] = len(self.docs) + 1
        self.docs[key] = stored

    async def update_one(self, query, update, upsert=False):
        self.update_one_calls.append((dict(query), upsert))
        if self._side_effects:
            exc = self._side_effects.pop(0)
            if exc is not None:
                raise exc
        for doc in self.docs.values():
            if all(doc.get(k) == v for k, v in query.items()):
                doc.update(update.get("$set", {}))
                return _UpdateResult(None)
        if upsert:
            new = dict(query)
            new.update(update.get("$set", {}))
            new["_id"] = len(self.docs) + 1
            self.docs[(new["shop"], new["url"], new["size"])] = new
            return _UpdateResult(new["_id"])
        return _UpdateResult(None)


def _seed(sku, in_stock=True, _id=1):
    doc = sku.model_dump()
    doc["_id"] = _id
    doc["in_stock"] = in_stock
    return {("carhartt_wip", sku.url, sku.size): doc}


@pytest.mark.asyncio
async def test_upsert_skus_inserts_new_sku_in_one_round_trip():
    collection = _UpsertCollection()
    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([_make_sku()])

    assert (ins, upd, failed) == (1, 0, 0)
    assert len(collection.docs) == 1
    # No pre-read and no insert_one: exactly one upsert per SKU.
    assert collection.find_one_calls == 0
    assert collection.insert_one_calls == 0
    assert collection.update_one_calls == [
        ({"shop": "carhartt_wip", "url": _make_sku().url, "size": "M"}, True)
    ]


@pytest.mark.asyncio
async def test_upsert_skus_refreshes_existing_sku_in_place():
    sku = _make_sku()
    collection = _UpsertCollection(_seed(sku, in_stock=False))
    refreshed = sku.model_copy(update={"in_stock": True})

    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([refreshed])

    assert (ins, upd, failed) == (0, 1, 0)
    assert len(collection.docs) == 1
    assert collection.find_one_calls == 0
    assert collection.insert_one_calls == 0
    assert next(iter(collection.docs.values()))["in_stock"] is True


@pytest.mark.asyncio
async def test_upsert_skus_mixed_new_and_existing_counts():
    existing = _make_sku(size="M")
    new = _make_sku(size="L")
    collection = _UpsertCollection(_seed(existing))

    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([existing, new])

    assert (ins, upd, failed) == (1, 1, 0)
    assert len(collection.docs) == 2
    assert ("carhartt_wip", new.url, "L") in collection.docs


@pytest.mark.asyncio
async def test_upsert_skus_duplicate_key_race_falls_back_to_plain_update():
    sku = _make_sku()
    # The row exists (as if a concurrent writer inserted it) and the first
    # upsert raises DuplicateKeyError; the fallback must refresh it.
    collection = _UpsertCollection(
        _seed(sku, in_stock=True),
        update_side_effects=[DuplicateKeyError("duplicate key race")],
    )
    stale = sku.model_copy(update={"in_stock": False})

    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([stale])

    assert (ins, upd, failed) == (0, 1, 0)
    assert len(collection.docs) == 1
    # First attempt is the upsert; the fallback is a plain update.
    assert collection.update_one_calls[0][1] is True
    assert collection.update_one_calls[1][1] is False
    assert next(iter(collection.docs.values()))["in_stock"] is False


@pytest.mark.asyncio
async def test_upsert_skus_one_failure_does_not_block_later_skus():
    first = _make_sku(size="M")
    second = _make_sku(size="L")
    collection = _UpsertCollection(update_side_effects=[RuntimeError("boom")])

    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([first, second])

    assert (ins, upd, failed) == (1, 0, 1)
    # The second SKU was still processed and persisted.
    assert len(collection.docs) == 1
    assert ("carhartt_wip", second.url, "L") in collection.docs


@pytest.mark.asyncio
async def test_upsert_skus_validation_error_counts_as_failed():
    try:
        SKU.model_validate({})
    except ValidationError as exc:  # a real pydantic ValidationError instance
        captured = exc
    collection = _UpsertCollection(update_side_effects=[captured])

    with patch("app.scrapers.carhartt.get_sku_collection", new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([_make_sku()])

    assert (ins, upd, failed) == (0, 0, 1)
    assert collection.docs == {}


# ---------------------------------------------------------------------------
# Bounded product concurrency (run_scraper)
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self, name="page"):
        self.name = name
        self._closed = False
        self.close_calls = 0
        self.default_timeout = None

    def is_closed(self):
        return self._closed

    async def close(self):
        self.close_calls += 1
        self._closed = True

    def set_default_timeout(self, ms):
        self.default_timeout = ms

    async def route(self, *args, **kwargs):
        return None


class _FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False

    async def new_page(self):
        page = _FakePage(f"page{len(self.pages) + 1}")
        self.pages.append(page)
        return page

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.closed = False

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


class _FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _setup_run(monkeypatch, urls):
    """Run ``run_scraper`` fully offline: fake browser stack, fixed URLs, no DB.

    Returns the shared fake stack so tests can assert resource cleanup.
    """
    context = _FakeContext()
    browser = _FakeBrowser()
    playwright = _FakePlaywright()
    discovery = _FakePage("discovery")

    async def _create():
        return playwright, browser, context, discovery

    async def _fetch(page, limit=None):
        return list(urls[:limit] if limit else urls)

    monkeypatch.setattr(carhartt, "_create_scrape_page", _create)
    monkeypatch.setattr(carhartt, "fetch_sitemap_urls", _fetch)
    monkeypatch.setattr(carhartt, "block_heavy_resources", AsyncMock())
    monkeypatch.setattr(carhartt, "_sleep", AsyncMock())  # no real retry delays
    monkeypatch.setattr(carhartt, "DEFAULT_DELAY_MS", 0)
    monkeypatch.setattr(
        carhartt,
        "db_counts",
        AsyncMock(return_value={
            "db_total_skus": 0,
            "db_unique_urls": 0,
            "db_in_stock": 0,
            "db_out_stock": 0,
        }),
    )
    return SimpleNamespace(
        context=context, browser=browser, playwright=playwright, discovery=discovery
    )


def _urls(n):
    return [f"https://www.carhartt-wip.com/en-de/p/product-{i}" for i in range(n)]


def _counting_attempt(state, delay=0.02, sku_for=None):
    """A fake ``scrape_attempt`` that tracks how many run concurrently."""
    async def attempt(page, url):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        try:
            await asyncio.sleep(delay)
            skus = sku_for(url) if sku_for else [_make_sku(url=url)]
            return skus, ""
        finally:
            state["live"] -= 1
    return attempt


def test_default_concurrency_is_conservative():
    assert MAX_CONCURRENT_PRODUCTS == 3
    assert MAX_CONCURRENCY_LIMIT == 10


@pytest.mark.asyncio
async def test_run_scraper_never_exceeds_concurrency_limit(monkeypatch):
    urls = _urls(10)
    _setup_run(monkeypatch, urls)
    state = {"live": 0, "peak": 0}

    monkeypatch.setattr(carhartt, "scrape_attempt", _counting_attempt(state))
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    stats = await run_scraper(max_products=None, concurrency=3)

    assert state["peak"] == 3           # the limit is reached but never exceeded
    assert stats["products_processed"] == 10
    assert stats["successful"] == 10
    assert stats["failed"] == 0


@pytest.mark.asyncio
async def test_run_scraper_concurrency_is_clamped_to_hard_cap(monkeypatch):
    urls = _urls(MAX_CONCURRENCY_LIMIT + 4)
    _setup_run(monkeypatch, urls)
    state = {"live": 0, "peak": 0}

    monkeypatch.setattr(carhartt, "scrape_attempt", _counting_attempt(state))
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    stats = await run_scraper(max_products=None, concurrency=999)

    assert state["peak"] <= MAX_CONCURRENCY_LIMIT   # defensive cap holds
    assert stats["successful"] == len(urls)


@pytest.mark.asyncio
async def test_run_scraper_creates_a_fixed_worker_pool(monkeypatch):
    # A large catalogue must not spawn one task per URL: the pool is capped.
    urls = _urls(30)
    _setup_run(monkeypatch, urls)
    task_ids = set()

    async def attempt(page, url):
        task_ids.add(id(asyncio.current_task()))
        await asyncio.sleep(0)
        return [], ""

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    stats = await run_scraper(max_products=None, concurrency=3)

    assert len(task_ids) == 3            # a fixed pool, never 30 tasks
    assert stats["products_processed"] == 30


@pytest.mark.asyncio
async def test_run_scraper_concurrency_zero_behaves_sequentially(monkeypatch):
    urls = _urls(3)
    _setup_run(monkeypatch, urls)
    state = {"live": 0, "peak": 0}

    monkeypatch.setattr(carhartt, "scrape_attempt", _counting_attempt(state, delay=0.01))
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    stats = await run_scraper(max_products=None, concurrency=0)

    assert state["peak"] == 1           # clamped up to at least one
    assert stats["successful"] == 3


@pytest.mark.asyncio
async def test_run_scraper_pages_isolated_and_closed(monkeypatch):
    urls = _urls(6)
    h = _setup_run(monkeypatch, urls)
    in_flight = set()
    overlap = {"peak": 0}

    async def attempt(page, url):
        in_flight.add(id(page))
        overlap["peak"] = max(overlap["peak"], len(in_flight))
        try:
            await asyncio.sleep(0.02)
            return [_make_sku(url=url)], ""
        finally:
            in_flight.discard(id(page))

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    await run_scraper(max_products=None, concurrency=3)

    # Distinct pages were used simultaneously (isolated page per product)...
    assert overlap["peak"] >= 2
    assert len(h.context.pages) >= overlap["peak"]
    # ...and every page is closed exactly once; the sitemap page too.
    assert all(p.close_calls == 1 for p in h.context.pages)
    assert h.discovery.close_calls == 1
    assert h.context.closed and h.browser.closed and h.playwright.stopped


@pytest.mark.asyncio
async def test_run_scraper_closes_page_after_failure_and_respects_retry_limit(monkeypatch):
    urls = _urls(2)
    h = _setup_run(monkeypatch, urls)
    calls = {"n": 0}

    async def attempt(page, url):
        calls["n"] += 1
        raise RuntimeError("net::ERR_CONNECTION_RESET")

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    upsert = AsyncMock(return_value=(1, 0, 0))
    monkeypatch.setattr(carhartt, "upsert_skus", upsert)

    stats = await run_scraper(max_products=None, concurrency=2)

    assert calls["n"] == 2 * MAX_ATTEMPTS_PER_URL   # retry limit unchanged
    assert stats["failed"] == 2
    assert stats["successful"] == 0
    assert stats["skus_failed"] == 0
    upsert.assert_not_called()
    # Each failed attempt opened its own page and every page was cleaned up.
    assert len(h.context.pages) == 2 * MAX_ATTEMPTS_PER_URL
    assert all(p.close_calls == 1 for p in h.context.pages)


@pytest.mark.asyncio
async def test_run_scraper_one_failure_does_not_cancel_others(monkeypatch):
    urls = _urls(5)
    bad = urls[2]
    _setup_run(monkeypatch, urls)

    async def attempt(page, url):
        if url == bad:
            raise RuntimeError("net::ERR_FAILED")
        return [_make_sku(url=url)], ""

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    stats = await run_scraper(max_products=None, concurrency=3)

    assert stats["products_processed"] == 5
    assert stats["successful"] == 4
    assert stats["failed"] == 1
    assert stats["failed_urls"] == [(bad, "network_error")]


@pytest.mark.asyncio
async def test_run_scraper_retryable_reason_reuses_page_then_succeeds(monkeypatch):
    urls = _urls(1)
    h = _setup_run(monkeypatch, urls)
    seen = []

    async def attempt(page, url):
        seen.append(id(page))
        if len(seen) < 2:
            return [], "http_503"
        return [_make_sku(url=url)], ""

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    stats = await run_scraper(max_products=None, concurrency=1)

    assert stats["successful"] == 1
    assert len(seen) == 2
    assert seen[0] == seen[1]                # retryable failure reuses the same page
    assert len(h.context.pages) == 1
    assert h.context.pages[0].close_calls == 1


@pytest.mark.asyncio
async def test_run_scraper_closes_pages_on_cancellation(monkeypatch):
    urls = _urls(6)
    h = _setup_run(monkeypatch, urls)
    started = asyncio.Event()

    async def attempt(page, url):
        started.set()
        await asyncio.sleep(30)              # hang until the run is cancelled
        return [_make_sku(url=url)], ""

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    task = asyncio.ensure_future(run_scraper(max_products=None, concurrency=3))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert h.context.pages, "expected at least one worker page"
    assert all(p.close_calls == 1 for p in h.context.pages)
    assert h.context.closed and h.browser.closed and h.playwright.stopped


@pytest.mark.asyncio
async def test_run_scraper_counts_and_no_duplicate_identities(monkeypatch):
    urls = _urls(4)
    _setup_run(monkeypatch, urls)
    collection = _UpsertCollection()

    async def attempt(page, url):
        return [_make_sku(url=url, size="M")], ""

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    # Real upsert_skus path, in-memory collection: identity is (shop, url, size).
    monkeypatch.setattr(
        carhartt, "get_sku_collection", AsyncMock(return_value=collection)
    )

    first = await run_scraper(max_products=None, concurrency=3)
    assert first["skus_inserted"] == 4
    assert first["skus_updated"] == 0
    assert len(collection.docs) == 4

    second = await run_scraper(max_products=None, concurrency=3)
    assert second["skus_inserted"] == 0
    assert second["skus_updated"] == 4
    assert len(collection.docs) == 4          # no duplicate identities created


@pytest.mark.asyncio
async def test_run_scraper_preserves_per_size_stock(monkeypatch):
    urls = _urls(1)
    _setup_run(monkeypatch, urls)
    collection = _UpsertCollection()

    skus = [
        _make_sku(url=urls[0], size="M"),
        _make_sku(url=urls[0], size="L").model_copy(update={"in_stock": False}),
    ]

    async def attempt(page, url):
        return skus, ""

    monkeypatch.setattr(carhartt, "scrape_attempt", attempt)
    monkeypatch.setattr(
        carhartt, "get_sku_collection", AsyncMock(return_value=collection)
    )

    stats = await run_scraper(max_products=None, concurrency=2)

    assert stats["skus_inserted"] == 2
    stock = {(d["url"], d["size"]): d["in_stock"] for d in collection.docs.values()}
    assert stock[(urls[0], "M")] is True
    assert stock[(urls[0], "L")] is False


@pytest.mark.asyncio
async def test_run_scraper_telemetry_totals_and_final_progress(monkeypatch):
    urls = _urls(3)
    _setup_run(monkeypatch, urls)
    progress_calls = []

    def spy(idx, total, stats, started):
        progress_calls.append((idx, total))

    monkeypatch.setattr(carhartt, "_print_progress", spy)
    monkeypatch.setattr(carhartt, "scrape_attempt", _counting_attempt({"live": 0, "peak": 0}, delay=0.01))
    monkeypatch.setattr(carhartt, "upsert_skus", AsyncMock(return_value=(1, 0, 0)))

    stats = await run_scraper(max_products=None, concurrency=3)

    assert stats["products_discovered"] == 3
    assert stats["products_processed"] == 3
    assert stats["successful"] == 3
    assert isinstance(stats["elapsed_seconds"], float)
    assert stats["elapsed_seconds"] >= 0
    # A single final progress line reports the complete total.
    assert progress_calls[-1] == (3, 3)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
