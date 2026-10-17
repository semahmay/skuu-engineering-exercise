"""Veja own-webshop scraper (Salesforce Commerce Cloud storefront).

Discovery: https://www.veja-store.com/{locale}/sitemap_index.xml -> *-product.xml.
The visible DOM is server-rendered, so per-size stock is read from the raw HTML
(the ``<li>`` wrapping each size button), with no per-size requests needed.

Reuses the generic pieces from the Carhartt scraper (upsert, failure
classification, cookie handling, retry/recovery/progress patterns) without
modifying it.
"""

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import List, Optional, Tuple

import httpx
from playwright.async_api import async_playwright, Page, Browser, BrowserContext

from app.models.sku import SKU
from app.database import get_sku_collection
from app.scrapers.carhartt import accept_cookies, classify_failure, upsert_skus

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


SHOP_ID = "veja"
BRAND = "Veja"
BASE_URL = "https://www.veja-store.com"
LOCALE = "en_ie"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_DELAY_MS = 1200
PAGE_WAIT_MS = 1000
MAX_PRODUCTS_DEFAULT = 5
MAX_ATTEMPTS_PER_URL = 3   # bounded: 1 try + max 2 retries
PROGRESS_EVERY = 25

# Deterministic outcomes: the page loaded and rendered, so retrying cannot help.
PERMANENT_REASONS = ("", "http_404", "no_json_ld", "no_size_selector", "stock_unresolved")


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class ProductData:
    title: str
    code: str
    price: float
    currency: str
    availability: str
    json_ld_sizes: List[str]


@dataclass
class SizeOption:
    """One size button.

    ``size_code`` is the underlying variant value (``data-value``, e.g. ``35-C``,
    ``001-C``, ``115-C``); ``size_label`` is the displayed size
    (``data-display-value``, e.g. ``35``, ``O.S``, ``115``). Stock comes from the
    wrapping ``<li>`` class.
    """

    size_code: str
    size_label: str
    li_class: str
    btn_class: str
    data_url: str = ""
    resolved: Optional[bool] = None

    @property
    def in_stock(self) -> Optional[bool]:
        if self.resolved is not None:
            return self.resolved
        if not self.li_class.strip():
            return None  # no wrapping <li>: ambiguous, caller may use AJAX fallback
        tokens = set(self.li_class.split())
        if "disabled" in tokens or "sold-out" in tokens:
            return False
        return True


# ---------------------------------------------------------------------------
# Sitemap discovery (pure + fetch helpers)
# ---------------------------------------------------------------------------

def parse_sitemap_index(xml: str) -> List[str]:
    """Return all ``<loc>`` URLs from a sitemap index / urlset."""
    return [loc.strip() for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", xml, re.S)]


def extract_product_urls(xml: str, locale: str = LOCALE) -> List[str]:
    """Keep only ``/{locale}/p/<slug>-<CODE>.html`` product URLs."""
    pattern = re.compile(
        r"https://www\.veja-store\.com/" + re.escape(locale) + r"/p/[^<\s]+?\.html"
    )
    return pattern.findall(xml)


def extract_code(url: str) -> str:
    """Trailing product code in ``...-<CODE>.html`` (uppercased)."""
    m = re.search(r"-([A-Za-z0-9]+)\.html(?:\?|$)", url)
    if m:
        return m.group(1).upper()
    # Fallback: last path token before .html
    m2 = re.search(r"/?([^/]+)\.html(?:\?|$)", url)
    if m2:
        token = m2.group(1)
        # if no dash, token itself may be the code
        m3 = re.search(r"([A-Z0-9]{6,})$", token, re.I)
        if m3:
            return m3.group(1).upper()
        return token.upper()
    return ""


def dedupe_product_urls(urls: List[str]) -> List[str]:
    """Collapse locale duplicates: one URL per trailing product code (first wins)."""
    seen = set()
    unique = []
    for url in urls:
        code = extract_code(url)
        if code:
            if code in seen:
                continue
            seen.add(code)
        unique.append(url)
    return unique


async def _http_get_text(url: str, retries: int = 3, timeout: int = 60) -> str:
    """GET a sitemap with bounded retries. Raises RuntimeError if exhausted."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"}
    last = "unknown"
    for attempt in range(1, retries + 1):
        try:
            async with httpx.AsyncClient(
                headers=headers, follow_redirects=True, timeout=timeout
            ) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.text
                last = f"http_{response.status_code}"
        except Exception as e:  # network, timeout, DNS...
            last = classify_failure(e)
        if attempt < retries:
            await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"sitemap fetch failed: {last} ({url})")


async def fetch_product_urls(
    locale: str = LOCALE, limit: Optional[int] = None
) -> List[str]:
    """Discover every product URL for one locale via the SFCC sitemap index."""
    index_url = f"{BASE_URL}/{locale}/sitemap_index.xml"
    index_xml = await _http_get_text(index_url)
    sitemap_urls = [u for u in parse_sitemap_index(index_xml) if "product" in u]
    if not sitemap_urls:
        sitemap_urls = [u for u in parse_sitemap_index(index_xml) if u.endswith(".xml")]

    all_urls: List[str] = []
    for sitemap in sitemap_urls:
        xml = await _http_get_text(sitemap)
        all_urls.extend(extract_product_urls(xml, locale))

    urls = dedupe_product_urls(all_urls)
    logger.info(
        "Discovered %d product URLs for %s (from %d sitemap files)",
        len(urls), locale, len(sitemap_urls),
    )
    if limit:
        urls = urls[:limit]
    return urls


# ---------------------------------------------------------------------------
# HTML parsing (pure)
# ---------------------------------------------------------------------------

class _VejaDomParser(HTMLParser):
    """Collect size/colour buttons and their wrapping ``<li>`` class."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._li_stack: List[str] = []
        self.size_buttons: List[dict] = []
        self.colour_buttons: List[dict] = []

    def handle_starttag(self, tag, attrs):
        ad = dict(attrs)
        if tag == "li":
            self._li_stack.append(ad.get("class", "") or "")
        elif tag == "button":
            data_type = ad.get("data-type")
            if data_type in ("size", "color"):
                record = {
                    "data_value": ad.get("data-value", "") or "",
                    "display": ad.get("data-display-value", "") or "",
                    "li_class": self._li_stack[-1] if self._li_stack else "",
                    "btn_class": ad.get("class", "") or "",
                    "data_url": ad.get("data-url", "") or "",
                }
                if data_type == "size":
                    self.size_buttons.append(record)
                else:
                    self.colour_buttons.append(record)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "li" and self._li_stack:
            self._li_stack.pop()


def _parse_buttons(html: str) -> _VejaDomParser:
    parser = _VejaDomParser()
    parser.feed(html)
    return parser


def extract_size_options(html: str) -> Tuple[List[SizeOption], int]:
    """Return (options, duplicate_count).

    The list is rendered up to three times (desktop / mobile /
    store-availability); the desktop copy is preferred and duplicates are
    collapsed by ``data-value``.
    """
    parser = _parse_buttons(html)

    def _is_desktop(button: dict) -> bool:
        tokens = set(button["btn_class"].split())
        return "mobile" not in tokens and "store-availability" not in tokens

    # Prefer the desktop copy, but keep every copy available for the fallback
    # and for duplicate accounting.
    desktop = [b for b in parser.size_buttons if _is_desktop(b)]
    others = [b for b in parser.size_buttons if not _is_desktop(b)]
    chosen = desktop + others

    seen = set()
    options: List[SizeOption] = []
    duplicates = 0
    for b in chosen:
        code = b["data_value"]
        if not code:
            continue
        if code in seen:
            duplicates += 1
            continue
        seen.add(code)
        options.append(
            SizeOption(
                size_code=code,
                size_label=b["display"],
                li_class=b["li_class"],
                btn_class=b["btn_class"],
                data_url=b["data_url"],
            )
        )
    return options, duplicates


def extract_colour(html: str) -> str:
    """Display value of the currently selected colour swatch (if any)."""
    parser = _parse_buttons(html)
    for b in parser.colour_buttons:
        if "selected" in b["li_class"].split():
            return b["display"] or b["data_value"]
    for b in parser.colour_buttons:
        if b["display"]:
            return b["display"]
    return ""


def _iter_json_ld_products(data):
    if isinstance(data, list):
        for item in data:
            yield from _iter_json_ld_products(item)
    elif isinstance(data, dict):
        if data.get("@type") == "Product":
            yield data
        elif isinstance(data.get("@graph"), list):
            for item in data["@graph"]:
                yield from _iter_json_ld_products(item)


def find_json_ld_product(html: str) -> Optional[dict]:
    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.S | re.I,
    )
    for raw in blocks:
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for item in _iter_json_ld_products(data):
            return item
    return None


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_size_list(value) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    if value:
        return [str(value)]
    return []


def parse_product_metadata(html: str) -> Optional[ProductData]:
    item = find_json_ld_product(html)
    if not item:
        return None
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return ProductData(
        title=item.get("name", "") or "",
        code=item.get("sku") or item.get("mpn") or "",
        price=_to_float(offers.get("price")),
        currency=offers.get("priceCurrency") or "EUR",
        availability=offers.get("availability", "") or "",
        json_ld_sizes=_as_size_list(item.get("size")),
    )


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", value)).strip()


def parse_product_dom_metadata(html: str, url: str, default_currency: str = "EUR") -> Optional[ProductData]:
    """Fallback metadata for pages that render no JSON-LD.

    Some archived / "notify me" products (e.g. the CONDOR 3 line) expose the
    title, product code and price only in the DOM / analytics payload. Returns
    ``None`` when a usable title+price cannot be found, so the caller can still
    record ``no_json_ld``.
    """
    title = ""
    m = re.search(
        r'<h1[^>]*class="[^"]*product-name[^"]*"[^>]*>(.*?)</h1>', html, re.S | re.I
    )
    if m:
        title = _clean_text(m.group(1))
    if not title:
        mt = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if mt:
            title = re.sub(r"\s*\|\s*VEJA.*$", "", _clean_text(mt.group(1))).strip()

    code_m = re.search(r'"item_id"\s*:\s*"([^"]+)"', html)
    code = code_m.group(1) if code_m else extract_code(url)

    price_m = re.search(r'"price"\s*:\s*(\d+(?:\.\d+)?)', html)
    price = float(price_m.group(1)) if price_m else 0.0

    cur_m = re.search(r'"currency"\s*:\s*"([A-Za-z]{3})"', html)
    currency = cur_m.group(1).upper() if cur_m else default_currency

    if not title or price <= 0:
        return None
    return ProductData(
        title=title,
        code=code,
        price=price,
        currency=currency,
        availability="",
        json_ld_sizes=[],
    )


def build_skus(
    product: ProductData,
    options: List[SizeOption],
    url: str,
    scraped_at: Optional[datetime] = None,
) -> List[SKU]:
    """Build one SKU per size that has a known stock state.

    The DTC colourway is already spelled out in ``product.title``, so the
    ``colour`` field is left empty. This keeps the composite identity
    (``normalize(title + colour)``) equal to that of retailers which publish the
    colourway inside the title, instead of splitting every match on a redundant
    trailing colour token.
    """
    moment = scraped_at or datetime.utcnow()
    skus: List[SKU] = []
    for opt in options:
        stock = opt.in_stock
        if stock is None:
            continue
        skus.append(
            SKU(
                shop=SHOP_ID,
                brand=BRAND,
                title=product.title,
                colour="",
                size=opt.size_label or opt.size_code,
                price=product.price,
                currency=product.currency,
                barcode=None,  # Veja exposes no GTIN/EAN/UPC
                in_stock=stock,
                url=url,
                scraped_at=moment,
            )
        )
    return skus


def parse_product_html(html: str, url: str) -> Tuple[List[SKU], str]:
    """Parse a product page into SKUs.

    Returns (skus, reason). ``reason == ""`` means success (skus may still be
    empty only when a product legitimately has no sizes). Failure reasons:
    ``no_json_ld``, ``no_size_selector``, ``stock_unresolved``.
    """
    product = parse_product_metadata(html)
    if not product:
        product = parse_product_dom_metadata(html, url)
        if product:
            logger.warning("No JSON-LD; using DOM metadata fallback: %s", url)
    if not product:
        return [], "no_json_ld"

    options, duplicates = extract_size_options(html)
    if duplicates:
        logger.debug("Collapsed %d duplicate size buttons", duplicates)

    if options:
        resolved = [o for o in options if o.in_stock is not None]
        if not resolved:
            return [], "stock_unresolved"
        return build_skus(product, resolved, url), ""

    # No size selector rendered.
    # Fallback: fully sold out products can drop the selector; JSON-LD then
    # carries the sizes (product-level availability = OutOfStock).
    if product.json_ld_sizes and "outofstock" in product.availability.lower():
        fallback = [
            SizeOption(size_code=s, size_label=s, li_class="", btn_class="", resolved=False)
            for s in product.json_ld_sizes
        ]
        logger.warning("No size selector; using JSON-LD sizes as sold out (%s)", url)
        return build_skus(product, fallback, url), ""

    return [], "no_size_selector"


# ---------------------------------------------------------------------------
# Playwright scraping
# ---------------------------------------------------------------------------

async def create_browser_context_with(playwright) -> tuple[Browser, BrowserContext]:
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 720},
        locale="en-IE",
    )
    return browser, context


async def _create_scrape_page():
    """Start a fresh Playwright + browser + page. Returns (pw, browser, ctx, page)."""
    playwright = await async_playwright().start()
    browser, context = await create_browser_context_with(playwright)
    page = await context.new_page()
    page.set_default_timeout(60000)
    return playwright, browser, context, page


async def _dispose(playwright=None, browser=None, context=None, page=None) -> None:
    for closer in (
        context.close if context else None,
        browser.close if browser else None,
        playwright.stop if playwright else None,
    ):
        if closer is None:
            continue
        try:
            await closer()
        except Exception:
            pass


async def _recreate_scrape_page(old):
    await _dispose(*old)
    last_exc = None
    for attempt in range(1, 4):
        try:
            return await _create_scrape_page()
        except Exception as e:
            last_exc = e
            logger.error("Browser restart attempt %d/3 failed: %s", attempt, e)
            await asyncio.sleep(5 * attempt)
    raise RuntimeError(f"could not restart browser: {last_exc}")


async def _resolve_option_stock(page: Page, data_url: str) -> Optional[bool]:
    """Authoritative per-size stock via the SFCC Product-Variation endpoint."""
    try:
        return await page.evaluate(
            """async (u) => {
                const r = await fetch(u, {headers: {'X-Requested-With': 'XMLHttpRequest'}});
                if (!r.ok) return null;
                const j = await r.json();
                return !!(j && j.product && j.product.available);
            }""",
            data_url,
        )
    except Exception as e:
        logger.warning("AJAX stock fallback failed: %s", classify_failure(e))
        return None


async def _resolve_unresolved(page: Page, html: str, url: str) -> List[SKU]:
    """Resolve ambiguous per-size stock via AJAX before building SKUs."""
    product = parse_product_metadata(html)
    if not product:
        return []
    options, _ = extract_size_options(html)
    for opt in options:
        if opt.in_stock is None and opt.data_url:
            opt.resolved = await _resolve_option_stock(page, opt.data_url)
    known = [o for o in options if o.in_stock is not None]
    return build_skus(product, known, url)


async def scrape_attempt(page: Page, url: str) -> Tuple[List[SKU], str]:
    """One attempt to scrape a product. Returns (skus, error_reason)."""
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    status = response.status if response else 0
    if status == 404:
        logger.warning("404 Not Found: %s", url)
        return [], "http_404"
    if status != 200:
        logger.warning("Non-200 (%s): %s", status, url)
        return [], f"http_{status}"

    await page.wait_for_timeout(PAGE_WAIT_MS)
    await accept_cookies(page)
    html = await page.content()

    skus, reason = parse_product_html(html, url)
    if reason == "stock_unresolved":
        skus = await _resolve_unresolved(page, html, url)
        reason = "stock_unresolved" if not skus else ""
    return skus, reason


# ---------------------------------------------------------------------------
# Run / reporting
# ---------------------------------------------------------------------------

def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def _print_progress(idx: int, total: int, stats: dict, started: float) -> None:
    elapsed = time.monotonic() - started
    rate = idx / elapsed if elapsed > 0 else 0.0
    eta = (total - idx) / rate if rate > 0 else 0.0
    print(
        f"--- progress {idx}/{total} | ok {stats['successful']} "
        f"| failed {stats['failed']} | 404 {stats['not_found_404']} "
        f"| new {stats['skus_inserted']} | updated {stats['skus_updated']} "
        f"| elapsed {_fmt_duration(elapsed)} | eta {_fmt_duration(eta)} ---",
        flush=True,
    )


async def db_counts() -> dict:
    collection = await get_sku_collection()
    return {
        "db_total_skus": await collection.count_documents({"shop": SHOP_ID}),
        "db_unique_urls": len(await collection.distinct("url", {"shop": SHOP_ID})),
        "db_in_stock": await collection.count_documents({"shop": SHOP_ID, "in_stock": True}),
        "db_out_stock": await collection.count_documents({"shop": SHOP_ID, "in_stock": False}),
    }


async def run_scraper(
    max_products: int = MAX_PRODUCTS_DEFAULT,
    locale: str = LOCALE,
) -> dict:
    started = time.monotonic()
    print(f"VEJA SCRAPER — starting (locale={locale}, max_products={max_products})", flush=True)

    stack = await _create_scrape_page()
    page = stack[3]

    stats = {
        "products_discovered": 0,
        "products_processed": 0,
        "successful": 0,
        "failed": 0,
        "not_found_404": 0,
        "skus_inserted": 0,
        "skus_updated": 0,
        "skus_failed": 0,
    }
    failed_urls: List[Tuple[str, str]] = []

    try:
        try:
            product_urls = await fetch_product_urls(locale=locale, limit=max_products)
        except Exception as e:
            print(f"FATAL: could not fetch sitemap: {e}", flush=True)
            stats["failed_urls"] = [("sitemap", classify_failure(e))]
            stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
            await _dispose(*stack)
            return stats

        stats["products_discovered"] = len(product_urls)
        total = len(product_urls)
        print(f"Discovered {total} product URLs", flush=True)

        for idx, url in enumerate(product_urls, 1):
            skus: List[SKU] = []
            reason = ""

            for attempt in range(1, MAX_ATTEMPTS_PER_URL + 1):
                try:
                    if page.is_closed():
                        raise RuntimeError("page_closed")
                    skus, reason = await scrape_attempt(page, url)
                except Exception as e:
                    reason = classify_failure(e)
                    skus = []
                    logger.warning(
                        "Attempt %d/%d failed for %s (%s); rebuilding browser",
                        attempt, MAX_ATTEMPTS_PER_URL, url, reason,
                    )
                    stack = await _recreate_scrape_page(stack)
                    page = stack[3]
                    await asyncio.sleep(2)
                    continue

                if reason in PERMANENT_REASONS:
                    break
                logger.warning(
                    "Attempt %d/%d: %s for %s", attempt, MAX_ATTEMPTS_PER_URL, reason, url,
                )
                if attempt < MAX_ATTEMPTS_PER_URL:
                    await asyncio.sleep(2 * attempt)

            stats["products_processed"] += 1
            title = skus[0].title if skus else url.rsplit("/", 1)[-1]

            if reason == "http_404":
                stats["not_found_404"] += 1
                failed_urls.append((url, "http_404"))
                print(f"[{idx}/{total}] {title} — 404 (skipped)", flush=True)
            elif reason and not skus:
                stats["failed"] += 1
                failed_urls.append((url, reason))
                print(f"[{idx}/{total}] {title} — FAILED ({reason})", flush=True)
            else:
                ins, upd, err = await upsert_skus(skus)
                stats["skus_inserted"] += ins
                stats["skus_updated"] += upd
                stats["skus_failed"] += err
                stats["successful"] += 1
                note = "" if skus else " (no sizes listed)"
                print(f"[{idx}/{total}] {title} — {ins} new, {upd} updated{note}", flush=True)

            if idx % PROGRESS_EVERY == 0 or idx == total:
                _print_progress(idx, total, stats, started)

            await asyncio.sleep(DEFAULT_DELAY_MS / 1000)

    finally:
        await _dispose(*stack)

    elapsed = time.monotonic() - started
    stats["elapsed_seconds"] = round(elapsed, 1)
    stats["failed_urls"] = failed_urls
    stats.update(await db_counts())

    print("", flush=True)
    print("=" * 60, flush=True)
    print("VEJA FULL SCRAPE RESULTS", flush=True)
    print("=" * 60, flush=True)
    print(f"Locale:                  {locale}", flush=True)
    print(f"Product URLs discovered: {stats['products_discovered']}", flush=True)
    print(f"Products processed:      {stats['products_processed']}", flush=True)
    print(f"Successful:              {stats['successful']}", flush=True)
    print(f"Failed:                  {stats['failed']}", flush=True)
    print(f"Skipped/404:             {stats['not_found_404']}", flush=True)
    print(f"Total Veja SKUs in DB:   {stats['db_total_skus']}", flush=True)
    print(f"New SKUs inserted:       {stats['skus_inserted']}", flush=True)
    print(f"Existing SKUs refreshed: {stats['skus_updated']}", flush=True)
    print(f"SKU write failures:      {stats['skus_failed']}", flush=True)
    print(f"Unique product URLs:     {stats['db_unique_urls']}", flush=True)
    print(f"In stock:                {stats['db_in_stock']}", flush=True)
    print(f"Out of stock:            {stats['db_out_stock']}", flush=True)
    print(f"Runtime:                 {_fmt_duration(elapsed)}", flush=True)
    if failed_urls:
        print("", flush=True)
        print(f"Failed URLs ({len(failed_urls)}):", flush=True)
        for f_url, f_reason in failed_urls:
            print(f"  {f_url} — {f_reason}", flush=True)
    print("=" * 60, flush=True)

    return stats


if __name__ == "__main__":
    import sys

    locale = LOCALE
    args = sys.argv[1:]
    if args and args[0].startswith("--locale="):
        locale = args[0].split("=", 1)[1]
        args = args[1:]
    max_products = int(args[0]) if args else MAX_PRODUCTS_DEFAULT
    if max_products > 50:
        logging.getLogger().setLevel(logging.WARNING)
    result = asyncio.run(run_scraper(max_products, locale=locale))
    print(json.dumps({k: v for k, v in result.items() if k != "failed_urls"}, indent=2), flush=True)
