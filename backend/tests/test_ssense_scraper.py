"""Tests for the SSENSE scraper (Playwright / Cloudflare-aware source).

No network and no browser: the pure extraction helpers (JSON-LD field mapping,
the size ``<select>`` parser, the ``Supplier color`` parser, brand/locale
mapping and the sitemap-loc parser) are exercised with in-memory fixtures, and
discovery is exercised with a patched sitemap fetcher.
"""

from datetime import datetime
import asyncio
import time

import pytest

from app.scrapers import ssense
from app.scrapers.ssense import (
    build_skus,
    canonical_brand,
    parse_colour,
    parse_size_options,
    parse_sitemap_locs,
    product_slug,
    rewrite_locale,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_stub_is_now_active():
    assert ssense.SHOP_ID == "ssense"
    assert ssense.STATUS == "active"
    assert ssense.CURRENCY == "EUR"
    assert ssense.LOCALE == "en-de"
    assert ssense.BASE_URL == "https://www.ssense.com"


def test_brand_scope_is_carhartt_and_veja_only():
    assert set(ssense.DESIGNER_SLUGS) == {"carhartt-work-in-progress", "veja"}
    assert set(ssense.DESIGNER_SLUGS.values()) == {"Carhartt WIP", "Veja"}


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,slug",
    [
        ("https://www.ssense.com/en-us/men/product/carhartt-work-in-progress/black-bay-tote/18605881",
         "carhartt-work-in-progress"),
        ("https://www.ssense.com/en-de/women/product/veja/off-white-campo-leather-sneakers/173722209",
         "veja"),
        ("https://www.ssense.com/en-us/men", ""),
        ("", ""),
    ],
)
def test_product_slug(url, slug):
    assert product_slug(url) == slug


def test_canonical_brand_prefers_slug():
    assert canonical_brand("carhartt-work-in-progress", "Carhartt Work In Progress") == "Carhartt WIP"
    assert canonical_brand("veja", "VEJA") == "Veja"


def test_canonical_brand_name_fallback():
    assert canonical_brand("", "Carhartt Work In Progress") == "Carhartt WIP"
    assert canonical_brand("", "VEJA") == "Veja"


def test_canonical_brand_unknown_is_empty():
    assert canonical_brand("some-other-brand", "Some Other Brand") == ""


def test_rewrite_locale_rewrites_only_locale_segment():
    us = "https://www.ssense.com/en-us/men/product/veja/off-white-campo-leather-sneakers/173722209"
    assert rewrite_locale(us, "en-de") == (
        "https://www.ssense.com/en-de/men/product/veja/off-white-campo-leather-sneakers/173722209"
    )


def test_rewrite_locale_leaves_non_ssense_urls_untouched():
    assert rewrite_locale("https://example.com/en-us/x", "en-de") == "https://example.com/en-us/x"


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------

def test_parse_sitemap_locs():
    xml = (
        '<urlset><url><loc>https://www.ssense.com/en-us/men/product/a/b/1</loc></url>'
        '<url><loc>https://www.ssense.com/en-us/men/product/c/d/2</loc></url></urlset>'
    )
    assert parse_sitemap_locs(xml) == [
        "https://www.ssense.com/en-us/men/product/a/b/1",
        "https://www.ssense.com/en-us/men/product/c/d/2",
    ]


def test_parse_sitemap_locs_empty():
    assert parse_sitemap_locs("") == []
    assert parse_sitemap_locs(None) == []


# ---------------------------------------------------------------------------
# Colour ("Supplier color: …") parsing
# ---------------------------------------------------------------------------

def test_parse_colour_simple():
    assert parse_colour("Buy this.\n\nSupplier color: Black") == "Black"


def test_parse_colour_multiline_and_whitespace():
    desc = "Non-stretch denim.\n\n Supplier color:   Blue River/Wax \n"
    assert parse_colour(desc) == "Blue River/Wax"


def test_parse_colour_case_insensitive():
    assert parse_colour("Blurb\nsupplier COLOR: Grey") == "Grey"


def test_parse_colour_absent_is_empty():
    assert parse_colour("No colour attribute here.") == ""
    assert parse_colour("") == ""
    assert parse_colour(None) == ""


# ---------------------------------------------------------------------------
# Size <select> parsing
# ---------------------------------------------------------------------------

def _opt(value, text, disabled=False):
    return {"value": value, "text": text, "disabled": disabled}


def test_parse_size_options_skips_placeholder():
    options = [
        _opt("", "SELECT A SIZE", disabled=True),
        _opt("43", "S - Only 2 remaining"),
        _opt("222", "XXL - Sold Out", disabled=True),
    ]
    assert parse_size_options(options) == [("S", True), ("XXL", False)]


def test_parse_size_options_keeps_waist_and_it_sizes():
    options = [
        _opt("292", "WAIST US 38 - Only 2 remaining"),
        _opt("209", "IT 41 - Only 1 remaining"),
    ]
    assert parse_size_options(options) == [("WAIST US 38", True), ("IT 41", True)]


def test_parse_size_options_size_without_stock_hint():
    assert parse_size_options([_opt("1", "One Size")]) == [("One Size", True)]


def test_parse_size_options_empty():
    assert parse_size_options([]) == []
    assert parse_size_options(None) == []


# ---------------------------------------------------------------------------
# SKU construction
# ---------------------------------------------------------------------------

def test_build_skus_multi_size_maps_fields_and_stock():
    skus = build_skus(
        brand="Carhartt WIP",
        title="Blue & White Short Sleeve Leavel Stripe Shirt",
        colour="Blue River/Wax",
        price=115.0,
        currency="EUR",
        supplier_sku="261111M192009",
        url="https://www.ssense.com/en-de/men/product/carhartt-work-in-progress/x/18849511",
        size_options=[("S", True), ("M", False), ("XL", True)],
        availability="https://schema.org/InStock",
    )
    assert [s.size for s in skus] == ["S", "M", "XL"]
    assert [s.in_stock for s in skus] == [True, False, True]
    first = skus[0]
    assert first.shop == "ssense"
    assert first.brand == "Carhartt WIP"
    assert first.colour == "Blue River/Wax"
    assert first.price == pytest.approx(115.0)
    assert first.currency == "EUR"
    assert first.barcode is None
    assert first.style_code == "261111M192009"
    assert first.url.endswith("/18849511")
    assert isinstance(first.scraped_at, datetime)


def test_build_skus_one_size_in_stock_from_availability():
    skus = build_skus(
        brand="Carhartt WIP",
        title="Black Bay Tote",
        colour="Black",
        price=86.0,
        currency="EUR",
        supplier_sku="261111M172001",
        url="https://www.ssense.com/en-de/men/product/carhartt-work-in-progress/x/18605881",
        size_options=[],
        availability="https://schema.org/InStock",
    )
    assert len(skus) == 1
    assert skus[0].size == "One Size"
    assert skus[0].in_stock is True


def test_build_skus_one_size_out_of_stock_from_availability():
    skus = build_skus(
        brand="Veja",
        title="White Campo Sneakers",
        colour="White",
        price=130.0,
        currency="EUR",
        supplier_sku="261610M237006",
        url="https://www.ssense.com/en-de/men/product/veja/x/1",
        size_options=[],
        availability="https://schema.org/OutOfStock",
    )
    assert len(skus) == 1
    assert skus[0].size == "One Size"
    assert skus[0].in_stock is False


def test_build_skus_gtin_shaped_supplier_code_becomes_barcode():
    skus = build_skus(
        brand="Veja",
        title="Test",
        colour="",
        price=1.0,
        currency="EUR",
        supplier_sku="1234567890123",
        url="u",
        size_options=[("S", True)],
        availability="InStock",
    )
    assert skus[0].barcode == "1234567890123"
    assert skus[0].style_code is None


def test_build_skus_empty_supplier_code_leaves_barcode_and_style_none():
    skus = build_skus(
        brand="Veja", title="T", colour="", price=1.0, currency="EUR",
        supplier_sku="", url="u", size_options=[("S", True)], availability="InStock",
    )
    assert skus[0].barcode is None
    assert skus[0].style_code is None


def test_build_skus_blank_colour_allowed():
    skus = build_skus(
        brand="Veja", title="T", colour="", price=1.0, currency="EUR",
        supplier_sku="261610M237006", url="u", size_options=[("IT 40", True)],
        availability="InStock",
    )
    assert skus[0].colour == ""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_product_urls_filters_brands_rewrites_locale_and_dedups(monkeypatch):
    shard = [
        "https://www.ssense.com/en-us/men/product/veja/off-white-campo-leather-sneakers/1",
        "https://www.ssense.com/en-us/men/product/gucci/black-bag/2",          # other brand
        "https://www.ssense.com/en-us/women/product/veja/white-sneakers/3",
        "https://www.ssense.com/en-us/men/product/veja/off-white-campo-leather-sneakers/1",  # dup
        "https://www.ssense.com/en-us/men/product/carhartt-work-in-progress/black-bay-tote/4",
    ]

    async def fake_locs(page, sitemap_url):
        return shard if sitemap_url == ssense.SITEMAP_URLS[0] else []

    monkeypatch.setattr(ssense, "_fetch_sitemap_locs", fake_locs)
    monkeypatch.setattr(ssense, "SITEMAP_DELAY_S", 0)

    urls = await ssense.fetch_product_urls(page=None, limit=None, locale="en-de")
    assert urls == [
        "https://www.ssense.com/en-de/men/product/veja/off-white-campo-leather-sneakers/1",
        "https://www.ssense.com/en-de/women/product/veja/white-sneakers/3",
        "https://www.ssense.com/en-de/men/product/carhartt-work-in-progress/black-bay-tote/4",
    ]


@pytest.mark.asyncio
async def test_fetch_product_urls_respects_limit(monkeypatch):
    async def fake_locs(page, sitemap_url):
        return [
            f"https://www.ssense.com/en-us/men/product/veja/p/{i}"
            for i in range(10)
        ]

    monkeypatch.setattr(ssense, "_fetch_sitemap_locs", fake_locs)
    monkeypatch.setattr(ssense, "SITEMAP_DELAY_S", 0)

    urls = await ssense.fetch_product_urls(page=None, limit=3, locale="en-de")
    assert len(urls) == 3
    assert all(u.startswith("https://www.ssense.com/en-de/") for u in urls)


@pytest.mark.asyncio
async def test_fetch_product_urls_propagates_fatal_sitemap_error(monkeypatch):
    async def boom(page, sitemap_url):
        raise RuntimeError("could not fetch sitemap x: http_503")

    monkeypatch.setattr(ssense, "_fetch_sitemap_locs", boom)
    monkeypatch.setattr(ssense, "SITEMAP_DELAY_S", 0)

    with pytest.raises(RuntimeError):
        await ssense.fetch_product_urls(page=None, limit=None, locale="en-de")


# ---------------------------------------------------------------------------
# Browser teardown (must never hang the run)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispose_swallows_closer_errors_and_runs_every_closer():
    calls = []

    class Closer:
        def __init__(self, name, exc=None):
            self.name = name
            self.exc = exc

        async def close(self):
            calls.append(self.name)
            if self.exc:
                raise self.exc

        async def stop(self):
            calls.append(self.name)
            if self.exc:
                raise self.exc

    context = Closer("context", RuntimeError("boom"))
    browser = Closer("browser")
    playwright = Closer("playwright")

    await ssense._dispose(playwright, browser, context, page=None)

    assert calls == ["context", "browser", "playwright"]


@pytest.mark.asyncio
async def test_dispose_is_bounded_when_a_closer_hangs(monkeypatch):
    monkeypatch.setattr(ssense, "CLOSE_TIMEOUT_S", 0.05)

    class Hang:
        async def close(self):
            await asyncio.sleep(3600)

    started = time.monotonic()
    await ssense._dispose(None, None, Hang(), None)
    assert time.monotonic() - started < 5


# ---------------------------------------------------------------------------
# Refresh-pipeline integration surface
# ---------------------------------------------------------------------------

def test_scraper_failed_recognises_sitemap_sentinel():
    from app.refresh.shops import scraper_failed

    stats = {"products_discovered": 0, "failed_urls": [("sitemap", "timeout")]}
    assert scraper_failed(stats) is True


def test_ssense_registered_in_refresh_registry():
    from app.refresh.shops import SHOPS, shop_ids

    assert "ssense" in shop_ids()
    spec = next(s for s in SHOPS if s.shop_id == "ssense")
    assert spec.module == "app.scrapers.ssense"
    assert spec.kind == "retailer"
