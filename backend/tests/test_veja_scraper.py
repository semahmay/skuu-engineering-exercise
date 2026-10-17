"""Deterministic tests for the Veja scraper (no network, no browser)."""

import json

import pytest
from pymongo.errors import DuplicateKeyError
from unittest.mock import AsyncMock, patch

from app.scrapers.veja import (
    SHOP_ID,
    dedupe_product_urls,
    extract_colour,
    extract_code,
    extract_product_urls,
    extract_size_options,
    parse_product_dom_metadata,
    parse_product_html,
    parse_product_metadata,
    parse_sitemap_index,
    SizeOption,
    ProductData,
    upsert_skus,
)
from app.models.sku import SKU


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

def _size_li(code, label, view="desktop", li_extra=""):
    classes = ["attributes__item-option"] + [c for c in li_extra.split() if c]
    li_class = " ".join(classes)
    return (
        f'<li class="{li_class}">'
        f'<button type="button" data-url="https://x/Product-Variation?dwvar_P_size={code}&pid=P"'
        f' data-value="{code}" data-display-value="{label}" data-type="size"'
        f' class="btn btn-primary swatch size-selector {view}">{label}</button>'
        f"</li>"
    )


def _colour_li(value, display, selected=False):
    cls = "attributes__item-option" + (" selected swiper-slide" if selected else "")
    return (
        f'<li class="{cls}">'
        f'<button type="button" data-value="{value}" data-display-value="{display}"'
        f' data-type="color" class="btn btn-primary swatch color-selector desktop">{display}</button>'
        f"</li>"
    )


def _json_ld(name="CAMPO CANVAS WHITE PIERRE", code="CA0103129", price="125.00",
             currency="EUR", availability="https://schema.org/InStock", sizes=None):
    product = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "mpn": code,
        "sku": code,
        "image": ["https://media.veja-store.com/img.jpg"],
        "offers": {
            "@type": "Offer",
            "price": price,
            "priceCurrency": currency,
            "availability": availability,
            "url": "https://www.veja-store.com/en_ie/p/x-CA0103129.html",
        },
    }
    if sizes is not None:
        product["size"] = sizes
    return f'<script type="application/ld+json">{json.dumps(product)}</script>'


def _page(li_blocks, json_ld=None):
    return (json_ld or _json_ld()) + "".join(li_blocks)


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------

def test_parse_sitemap_index():
    xml = (
        "<sitemapindex>"
        "<sitemap><loc>https://www.veja-store.com/fr_fr/sitemap_0-product.xml</loc></sitemap>"
        "<sitemap><loc>https://www.veja-store.com/fr_fr/sitemap_6-category.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    locs = parse_sitemap_index(xml)
    assert len(locs) == 2
    assert locs[0].endswith("sitemap_0-product.xml")


def test_extract_product_urls_filters_locale():
    xml = (
        "<urlset>"
        "<url><loc>https://www.veja-store.com/en_ie/p/campo-white-CA0103129.html</loc></url>"
        "<url><loc>https://www.veja-store.com/fr_fr/p/campo-blanc-CA0103129.html</loc></url>"
        "<url><loc>https://www.veja-store.com/en_ie/p/tote-AA0100020.html</loc></url>"
        "</urlset>"
    )
    urls = extract_product_urls(xml, "en_ie")
    assert len(urls) == 2
    assert all("/en_ie/p/" in u for u in urls)


def test_extract_code():
    assert extract_code("https://www.veja-store.com/en_ie/p/campo-white-CA0103129.html") == "CA0103129"
    assert extract_code("https://x/AA0100020.html") == "AA0100020"


def test_dedupe_product_urls_by_code():
    urls = [
        "https://www.veja-store.com/en_ie/p/campo-white-CA0103129.html",
        "https://www.veja-store.com/en_ie/p/campo-white-CA0103129.html?x=1",
        "https://www.veja-store.com/en_ie/p/tote-AA0100020.html",
    ]
    deduped = dedupe_product_urls(urls)
    assert deduped == [
        "https://www.veja-store.com/en_ie/p/campo-white-CA0103129.html",
        "https://www.veja-store.com/en_ie/p/tote-AA0100020.html",
    ]


# ---------------------------------------------------------------------------
# Size / stock extraction
# ---------------------------------------------------------------------------

def test_size_options_dedupe_three_views():
    html = "".join([
        _size_li("35-C", "35", "desktop"),
        _size_li("35-C", "35", "mobile"),
        _size_li("35-C", "35", "store-availability size"),
    ])
    options, duplicates = extract_size_options(html)
    assert len(options) == 1
    assert duplicates == 2
    assert options[0].size_code == "35-C"
    assert options[0].size_label == "35"


def test_stock_states():
    html = (
        _size_li("35-C", "35")                                    # in stock
        + _size_li("36-C", "36", li_extra="low-stock")            # low stock -> in
        + _size_li("37-C", "37", li_extra="disabled sold-out")    # out
    )
    options, _ = extract_size_options(html)
    by_code = {o.size_code: o for o in options}
    assert by_code["35-C"].in_stock is True
    assert by_code["36-C"].in_stock is True
    assert by_code["37-C"].in_stock is False


def test_size_option_missing_li_is_unknown():
    opt = SizeOption(size_code="35-C", size_label="35", li_class="", btn_class="")
    assert opt.in_stock is None


# ---------------------------------------------------------------------------
# Metadata / colour
# ---------------------------------------------------------------------------

def test_parse_product_metadata():
    meta = parse_product_metadata(_json_ld(price="125.00", currency="EUR"))
    assert isinstance(meta, ProductData)
    assert meta.title == "CAMPO CANVAS WHITE PIERRE"
    assert meta.code == "CA0103129"
    assert meta.price == 125.0
    assert meta.currency == "EUR"
    assert meta.json_ld_sizes == []


def test_extract_colour_selected():
    html = (
        _colour_li("BLACK_03129", "Black")
        + _colour_li("WHITE_PIERRE_03129", "White", selected=True)
    )
    assert extract_colour(html) == "White"


# ---------------------------------------------------------------------------
# parse_product_html
# ---------------------------------------------------------------------------

def test_parse_normal_mixed_stock():
    html = _page([
        _size_li("35-C", "35"),
        _size_li("36-C", "36", li_extra="low-stock"),
        _size_li("37-C", "37", li_extra="disabled sold-out"),
    ], json_ld=_json_ld() + _colour_li("WHITE_PIERRE_03129", "White", selected=True))

    skus, reason = parse_product_html(html, "https://www.veja-store.com/en_ie/p/x-CA0103129.html")
    assert reason == ""
    assert len(skus) == 3
    by_size = {s.size: s for s in skus}
    assert by_size["35"].in_stock is True
    assert by_size["36"].in_stock is True
    assert by_size["37"].in_stock is False
    assert all(s.shop == SHOP_ID for s in skus)
    assert all(s.brand == "Veja" for s in skus)
    assert all(s.currency == "EUR" for s in skus)
    assert all(s.barcode is None for s in skus)
    assert all(s.colour == "" for s in skus)


def test_parse_one_size():
    html = _page([_size_li("001-C", "O.S")])
    skus, reason = parse_product_html(html, "https://www.veja-store.com/en_ie/p/tote-AA0100020.html")
    assert reason == ""
    assert len(skus) == 1
    assert skus[0].size == "O.S"
    assert skus[0].in_stock is True


def test_parse_length_sizes():
    html = _page([
        _size_li("115-C", "115"),
        _size_li("125-C", "125"),
        _size_li("135-C", "135"),
    ])
    skus, reason = parse_product_html(html, "https://www.veja-store.com/en_ie/p/laces-LA01BLA01.html")
    assert reason == ""
    assert {s.size for s in skus} == {"115", "125", "135"}


def test_parse_all_sold_out_selector_present():
    html = _page([
        _size_li("35-C", "35", li_extra="disabled sold-out"),
        _size_li("36-C", "36", li_extra="disabled sold-out"),
    ])
    skus, reason = parse_product_html(html, "https://x/p/CA0103129.html")
    assert reason == ""
    assert len(skus) == 2
    assert all(s.in_stock is False for s in skus)


def test_parse_no_size_selector():
    html = _page([])
    skus, reason = parse_product_html(html, "https://x/p/CA0103129.html")
    assert skus == []
    assert reason == "no_size_selector"


def test_parse_fully_sold_out_no_selector_jsonld_fallback():
    html = _json_ld(availability="https://schema.org/OutOfStock", sizes=["35", "36"])
    skus, reason = parse_product_html(html, "https://x/p/CA0103129.html")
    assert reason == ""
    assert {s.size for s in skus} == {"35", "36"}
    assert all(s.in_stock is False for s in skus)


def test_parse_no_json_ld():
    html = _size_li("35-C", "35")
    skus, reason = parse_product_html(html, "https://x/p/CA0103129.html")
    assert skus == []
    assert reason == "no_json_ld"


def test_dom_metadata_fallback():
    html = (
        '<h1 class="product-name">CONDOR 3 ENGINEERED-MESH WHITE BLACK</h1>'
        '<script>dataLayer = [{"item_id":"CC2803578","item_variant":"WHITE_BLACK_03578",'
        '"price":144,"discount":36}];</script>'
    )
    meta = parse_product_dom_metadata(html, "https://x/p/condor-CC2803578.html")
    assert meta is not None
    assert meta.title == "CONDOR 3 ENGINEERED-MESH WHITE BLACK"
    assert meta.code == "CC2803578"
    assert meta.price == 144.0
    assert meta.currency == "EUR"


def test_parse_dom_fallback_builds_skus():
    html = (
        '<h1 class="product-name">CONDOR 3 ENGINEERED-MESH WHITE BLACK</h1>'
        '<script>dataLayer = [{"item_id":"CC2803578","price":144}];</script>'
        + _size_li("35-C", "35")
        + _size_li("36-C", "36", li_extra="disabled sold-out")
    )
    skus, reason = parse_product_html(html, "https://x/p/condor-CC2803578.html")
    assert reason == ""
    assert len(skus) == 2
    by_size = {s.size: s for s in skus}
    assert by_size["35"].in_stock is True
    assert by_size["36"].in_stock is False
    assert by_size["35"].price == 144.0


def test_dom_fallback_returns_none_without_price():
    html = '<h1 class="product-name">SOMETHING</h1>'
    assert parse_product_dom_metadata(html, "https://x/p/X.html") is None


def test_parse_stock_unresolved():
    html = _page(["<li><button data-value=\"35-C\" data-display-value=\"35\" data-type=\"size\"></button></li>"])
    skus, reason = parse_product_html(html, "https://x/p/CA0103129.html")
    assert skus == []
    assert reason == "stock_unresolved"


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


def _make_sku(size, in_stock=True, url="https://x/p/CA0103129.html"):
    return SKU(
        shop=SHOP_ID,
        brand="Veja",
        title="CAMPO",
        colour="White",
        size=size,
        price=125.0,
        currency="EUR",
        barcode=None,
        in_stock=in_stock,
        url=url,
    )


@pytest.mark.asyncio
async def test_upsert_inserts_then_refreshes_without_duplicates():
    collection = FakeCollection()
    with patch("app.scrapers.carhartt.get_sku_collection",
               new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([_make_sku("35"), _make_sku("36")])
        assert (ins, upd, failed) == (2, 0, 0)
        assert len(collection.docs) == 2

        # Second run: refresh in place, no new rows
        ins2, upd2, failed2 = await upsert_skus([_make_sku("35"), _make_sku("36", in_stock=False)])
        assert (ins2, upd2, failed2) == (0, 2, 0)
        assert len(collection.docs) == 2

    refreshed = {(d["url"], d["size"]): d for d in collection.docs.values()}
    assert refreshed[("https://x/p/CA0103129.html", "36")]["in_stock"] is False


@pytest.mark.asyncio
async def test_upsert_partial_existing():
    collection = FakeCollection()
    collection.docs[(SHOP_ID, "https://x/p/CA0103129.html", "35")] = {
        "_id": 1, "shop": SHOP_ID, "url": "https://x/p/CA0103129.html", "size": "35",
    }
    with patch("app.scrapers.carhartt.get_sku_collection",
               new=AsyncMock(return_value=collection)):
        ins, upd, failed = await upsert_skus([_make_sku("35"), _make_sku("37")])
    assert (ins, upd, failed) == (1, 1, 0)
    assert len(collection.docs) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
