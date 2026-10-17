"""SSENSE scraper — Playwright-based, Cloudflare-aware.

Retailer: https://www.ssense.com (multi-brand luxury).

Status: **implemented** (previously a documented Cloudflare block).

Access model
------------
SSENSE sits behind **Cloudflare Bot Management**. A plain HTTP client
(``httpx``, ``requests``) is answered **HTTP 403** with ``server: cloudflare``
and a ``cf-mitigated: challenge`` header on every route (category, product and
even the declared sitemaps). There is no Shopify ``/products.json`` feed.

An *ordinary browser session*, however, is permitted without any challenge
being served: headless Chromium with a normal Chrome user-agent executes the
Cloudflare JavaScript, receives the ``cf_clearance`` / ``__cf_ob`` bot cookies
and loads the page normally. This scraper therefore uses **Playwright** (the
same dependency and launch conventions already used by ``carhartt.py`` and
``veja.py``). No CAPTCHA solving, fingerprint spoofing, proxy, or external
service is involved — only a standard browser navigation.

Locale / currency
-----------------
``robots.txt`` disallows a long list of country locales but **allows** the core
ones, including ``/en-de`` (Germany). The ``en-de`` locale publishes prices in
**EUR**, which matches every other shop in this project (the previous stub
declared ``EUR``; the default ``en-us`` storefront would have published USD).
Product URLs are locale-tagged (``/en-us/…`` vs ``/en-de/…``) but the product
id and slugs are identical, so the discovery feed's locale segment is rewritten
to the scraper's locale.

Discovery
---------
Discovery uses the **product sitemap** declared in ``robots.txt``
(``Sitemap: https://www.ssense.com/sitemap.xml`` → ``sitemap_products_list_*.xml``),
which lists every product URL and is therefore complete. ``robots.txt``
*disallows* ``?page=`` and the ``isAjax``/``/api/`` load-more endpoints, so the
paginated designer pages cannot be crawled to completion; the sitemap is the
only robots-compliant source that covers the whole catalogue.

Scope
-----
Only products whose brand resolves to **Carhartt WIP** or **Veja** are
ingested, matching the brand scope of the rest of the project. SSENSE does
**not** carry Salomon (verified: no ``/designers/salomon`` page and zero
``/product/salomon/`` URLs in the sitemap).

Honesty rules
-------------
- Stock is read verbatim from the size ``<select>``: an ``<option>`` that is
  ``disabled`` is sold out, anything else is in stock. When a product exposes no
  size selector (one-size accessories), stock falls back to the product-level
  schema.org ``availability`` and the size is recorded as ``One Size``.
- Colour is taken from the site's own ``Supplier color: …`` attribute (surfaced
  at the end of ``og:description``); it is never parsed out of the product title
  and is left empty when the attribute is absent.
- ``barcode`` stays ``None`` — SSENSE publishes no GTIN. The supplier code from
  JSON-LD ``sku`` (e.g. ``242111M186039``) is stored as ``style_code`` for
  evidence and is deliberately **not** used as a matching identity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import Page

from app.models.sku import SKU
from app.database import get_sku_collection
from app.scrapers.carhartt import classify_failure, upsert_skus

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


SHOP_ID = "ssense"
BASE_URL = "https://www.ssense.com"
LOCALE = "en-de"          # EUR storefront; allowed by robots.txt
CURRENCY = "EUR"
STATUS = "active"

# SSENSE designer slug -> project brand. Brand is taken from the URL slug (the
# most stable identifier); the JSON-LD brand name is only a fallback.
DESIGNER_SLUGS: Dict[str, str] = {
    "carhartt-work-in-progress": "Carhartt WIP",
    "veja": "Veja",
}

SITEMAP_URLS: Tuple[str, ...] = tuple(
    f"{BASE_URL}/sitemap_products_list_{i}.xml" for i in range(5)
)
SITEMAP_SHARDS = len(SITEMAP_URLS)

MAX_PRODUCTS_DEFAULT = 5
MAX_ATTEMPTS_PER_URL = 3     # 1 try + up to 2 retries (bounded)
MAX_SITEMAP_ATTEMPTS = 3     # per sitemap shard
PROGRESS_EVERY = 25
PAGE_WAIT_MS = 2500
NAV_TIMEOUT_MS = 60000
SITEMAP_TIMEOUT_MS = 180000
SITEMAP_DELAY_S = 3.0        # be polite between large sitemap downloads
REQUEST_DELAY_S = 0.8        # be polite between product pages
CLOSE_TIMEOUT_S = 15         # hard cap on context/browser/playwright teardown
LAUNCH_TIMEOUT_S = 60        # hard cap on a browser (re)launch
ATTEMPT_TIMEOUT_S = 90       # hard cap on one product attempt (nav timeout + slack)

ONE_SIZE = "One Size"
SIZE_SELECTOR = "#size-dropdown"
GTIN_LENGTHS = frozenset((8, 12, 13, 14))

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Pure extraction helpers (browser-free, unit-testable)
# ---------------------------------------------------------------------------

def product_slug(url: str) -> str:
    """Designer slug from an SSENSE product URL (``.../product/<slug>/...``)."""
    m = re.search(r"/product/([^/]+)/", url or "")
    return m.group(1) if m else ""


def canonical_brand(slug: str, brand_name: str = "") -> str:
    """Project brand for a product, preferring the stable URL slug."""
    if slug in DESIGNER_SLUGS:
        return DESIGNER_SLUGS[slug]
    low = (brand_name or "").lower()
    if "carhartt" in low:
        return "Carhartt WIP"
    if "veja" in low:
        return "Veja"
    return ""


def rewrite_locale(url: str, locale: str) -> str:
    """Rewrite the ``/xx-yy/`` locale segment of an SSENSE URL."""
    return re.sub(
        r"^(https://www\.ssense\.com/)[a-z]{2}-[a-z]{2}/",
        rf"\g<1>{locale}/",
        url or "",
    )


def parse_sitemap_locs(xml: str) -> List[str]:
    """All ``<loc>`` URLs from a product sitemap document."""
    return re.findall(r"<loc>([^<]+)</loc>", xml or "")


def parse_colour(description: str) -> str:
    """Colour from the trailing ``Supplier color: …`` attribute, else ``''``.

    SSENSE appends the supplier colour to the product description (surfaced in
    ``og:description``). It is the only structured colour signal the site
    publishes; nothing is parsed from the title.
    """
    if not description:
        return ""
    m = re.search(r"Supplier\s+color\s*:\s*(.+?)\s*$", description, re.S | re.I)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def _gtin_shaped(value: str) -> bool:
    """True only for a *bare* GTIN (all digits, GTIN length)."""
    v = (value or "").strip()
    return v.isdigit() and len(v) in GTIN_LENGTHS


def parse_size_options(options: List[Dict[str, Any]]) -> List[Tuple[str, bool]]:
    """``[(size, in_stock), …]`` from the size ``<select>`` option records.

    The placeholder option (empty ``value``) is skipped. A real option's text is
    ``"<size> - <stock hint>"`` (e.g. ``"WAIST US 38 - Only 2 remaining"`` or
    ``"XXL - Sold Out"``); ``disabled`` marks a sold-out size.
    """
    parsed: List[Tuple[str, bool]] = []
    for option in options or []:
        value = str(option.get("value") or "").strip()
        if not value:
            continue  # placeholder "SELECT A SIZE"
        text = str(option.get("text") or "").strip()
        size = text.rsplit(" - ", 1)[0].strip() if " - " in text else text
        if not size:
            continue
        in_stock = not bool(option.get("disabled"))
        parsed.append((size, in_stock))
    return parsed


def build_skus(
    brand: str,
    title: str,
    colour: str,
    price: float,
    currency: str,
    supplier_sku: str,
    url: str,
    size_options: List[Tuple[str, bool]],
    availability: str,
    scraped_at: Optional[datetime] = None,
) -> List[SKU]:
    """Build SKU rows for one product, honouring per-size availability.

    Products with a size selector yield one SKU per size. Products with no
    selector (one-size accessories) yield a single ``One Size`` row whose stock
    comes from the product-level schema.org ``availability``.
    """
    moment = scraped_at or datetime.utcnow()

    # SSENSE publishes no GTIN; a bare numeric ``sku`` would be a barcode, but
    # in practice they are alphanumeric supplier codes stored as style_code.
    barcode = supplier_sku if _gtin_shaped(supplier_sku) else None
    style_code = None if barcode else (supplier_sku or None)

    def _sku(size: str, in_stock: bool) -> SKU:
        return SKU(
            shop=SHOP_ID,
            brand=brand,
            title=title,
            colour=colour,
            size=size,
            price=price,
            currency=currency,
            barcode=barcode,
            style_code=style_code,
            in_stock=in_stock,
            url=url,
            scraped_at=moment,
        )

    if size_options:
        return [_sku(size, in_stock) for size, in_stock in size_options]

    in_stock = "InStock" in (availability or "")
    return [_sku(ONE_SIZE, in_stock)]


# ---------------------------------------------------------------------------
# Browser session helpers (Playwright conventions shared with carhartt.py)
# ---------------------------------------------------------------------------

async def _block_heavy_resources(page: Page) -> None:
    """Abort images/fonts/media (keep CSS + JS so Cloudflare and React run)."""

    async def _block(route):
        # ``continue_``/``abort`` can race a navigation and raise
        # ``Invalid InterceptionId`` (or a page-closed error) for a request that
        # is no longer interceptable. That is benign — the request is simply not
        # blocked — so it must never surface as an unhandled event-loop error
        # (which would slow the run and trigger needless browser rebuilds).
        try:
            if route.request.resource_type in ("image", "font", "media"):
                await route.abort()
            else:
                await route.continue_()
        except Exception:  # noqa: BLE001
            pass

    await page.route("**/*", _block)


async def _accept_cookies(page: Page) -> bool:
    for selector in (
        'button:has-text("Accept all")',
        'button:has-text("Accept All")',
        'button:has-text("Alle akzeptieren")',
        'button:has-text("Akzeptieren")',
        'button:has-text("Accept")',
        "#onetrust-accept-btn-handler",
    ):
        try:
            btn = page.locator(selector).first
            if await btn.count() > 0:
                await btn.click()
                await page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False


async def create_browser_context_with(playwright):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="de-DE",
    )
    return browser, context


async def _create_scrape_page():
    """Fresh Playwright + browser + context + page.

    Returns ``(playwright, browser, context, page)``.
    """
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    browser, context = await create_browser_context_with(playwright)
    page = await context.new_page()
    await _block_heavy_resources(page)
    page.set_default_timeout(NAV_TIMEOUT_MS)
    return playwright, browser, context, page


async def _dispose(playwright=None, browser=None, context=None, page=None) -> None:
    # Teardown must never be able to hang the run: a wedged Chromium close can
    # wait forever, so each step is capped. Failures are swallowed on purpose —
    # a browser that refuses to close cleanly is not a scraping error.
    for closer in (
        context.close if context else None,
        browser.close if browser else None,
        playwright.stop if playwright else None,
    ):
        if closer is None:
            continue
        try:
            await asyncio.wait_for(closer(), timeout=CLOSE_TIMEOUT_S)
        except Exception:
            pass


async def _recreate_scrape_page(old):
    """Dispose the old browser stack and start a fresh one (bounded retries)."""
    await _dispose(*old)
    last_exc = None
    for attempt in range(1, 4):
        try:
            return await asyncio.wait_for(_create_scrape_page(), timeout=LAUNCH_TIMEOUT_S)
        except Exception as e:
            last_exc = e
            logger.error("Browser restart attempt %d/3 failed: %s", attempt, e)
            await asyncio.sleep(5 * attempt)
    raise RuntimeError(f"could not restart browser: {last_exc}")


# ---------------------------------------------------------------------------
# Page extraction
# ---------------------------------------------------------------------------

async def _is_challenge(page: Page) -> bool:
    """True when Cloudflare is showing a JS/CAPTCHA interstitial."""
    try:
        title = (await page.title() or "").lower()
    except Exception:
        return True
    if "just a moment" in title or "attention required" in title:
        return True
    try:
        return await page.evaluate(
            "() => !!document.querySelector('#challenge-form, .cf-chl-running, .cf-browser-verification')"
        )
    except Exception:
        return False


async def parse_json_ld(page: Page) -> Optional[dict]:
    """The schema.org ``Product`` object from JSON-LD, or ``None``."""
    for content in await page.eval_on_selector_all(
        'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
    ):
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
    return None


async def _extract_colour(page: Page) -> str:
    for selector in ('meta[property="og:description"]', 'meta[name="twitter:description"]'):
        try:
            content = await page.eval_on_selector(
                selector, "e => e.getAttribute('content') || ''"
            )
        except Exception:
            content = ""
        colour = parse_colour(content)
        if colour:
            return colour
    return ""


async def _extract_size_options(page: Page) -> List[Dict[str, Any]]:
    try:
        return await page.eval_on_selector_all(
            f"{SIZE_SELECTOR} option",
            "els => els.map(e => ({value: e.value || '', text: (e.textContent || '').trim(), "
            "disabled: !!e.disabled}))",
        )
    except Exception:
        return []


async def _canonical_url(page: Page, product: dict, url: str) -> str:
    offers = product.get("offers") or {}
    for candidate in (offers.get("url"), product.get("url")):
        if candidate:
            return str(candidate)
    try:
        link = await page.eval_on_selector(
            'link[rel="canonical"]', "e => e.getAttribute('href')"
        )
        if link:
            return link
    except Exception:
        pass
    return url


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def scrape_attempt(page: Page, url: str) -> Tuple[List[SKU], str]:
    """One attempt to scrape a product page.

    Returns ``(skus, error_reason)``. ``error_reason == ""`` means the page was
    read successfully. Reasons: ``http_404``, ``http_<code>``,
    ``cloudflare_challenge``, ``no_json_ld``, ``no_brand``.
    Raises on navigation/timeout/crash (caller handles browser rebuild).
    """
    response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    status = response.status if response else 0
    if status == 404:
        return [], "http_404"
    if status == 403:
        return [], "cloudflare_challenge"
    if status != 200:
        return [], f"http_{status}"

    await page.wait_for_timeout(PAGE_WAIT_MS)
    if await _is_challenge(page):
        return [], "cloudflare_challenge"
    await _accept_cookies(page)

    product = await parse_json_ld(page)
    if not product:
        return [], "no_json_ld"

    slug = product_slug(url)
    brand = canonical_brand(slug, (product.get("brand") or {}).get("name", ""))
    if not brand:
        return [], "no_brand"

    offers = product.get("offers") or {}
    colour = await _extract_colour(page)
    size_options = parse_size_options(await _extract_size_options(page))
    canonical = await _canonical_url(page, product, url)

    skus = build_skus(
        brand=brand,
        title=str(product.get("name") or "").strip(),
        colour=colour,
        price=_to_float(offers.get("price")),
        currency=str(offers.get("priceCurrency") or CURRENCY),
        supplier_sku=str(product.get("sku") or ""),
        url=canonical,
        size_options=size_options,
        availability=str(offers.get("availability") or ""),
    )
    logger.debug("Scraped %s -> %d SKUs (brand=%s)", url, len(skus), brand)
    return skus, ""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

async def _fetch_sitemap_locs(page: Page, sitemap_url: str) -> List[str]:
    """Fetch one product-sitemap shard, retrying transient failures.

    Raises when the shard cannot be read so the caller can treat catalogue
    discovery as a fatal failure instead of silently producing a partial set.
    """
    last = "unknown"
    for attempt in range(1, MAX_SITEMAP_ATTEMPTS + 1):
        try:
            response = await page.goto(
                sitemap_url, wait_until="domcontentloaded", timeout=SITEMAP_TIMEOUT_MS
            )
            status = response.status if response else 0
            if status != 200:
                last = f"http_{status}"
                logger.warning("Sitemap %s: %s (attempt %d/%d)",
                               sitemap_url, last, attempt, MAX_SITEMAP_ATTEMPTS)
                await asyncio.sleep(2 * attempt)
                continue
            content = await page.content()
            if "<urlset" not in content:
                last = "not_urlset"
                await asyncio.sleep(2 * attempt)
                continue
            locs = parse_sitemap_locs(content)
            logger.info("Sitemap %s: %d locs", sitemap_url, len(locs))
            return locs
        except Exception as e:  # noqa: BLE001
            last = classify_failure(e)
            logger.warning("Sitemap %s: %s (attempt %d/%d)",
                           sitemap_url, last, attempt, MAX_SITEMAP_ATTEMPTS)
            await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"could not fetch sitemap {sitemap_url}: {last}")


async def fetch_product_urls(page: Page, limit: Optional[int] = None,
                             locale: str = LOCALE) -> List[str]:
    """Deduplicated, brand-scoped, locale-rewritten product URLs.

    Reads the robots-declared product sitemap shards until ``limit`` products
    are collected (or every shard is read when ``limit`` is ``None``).
    """
    collected: List[str] = []
    seen = set()
    for idx, sitemap_url in enumerate(SITEMAP_URLS):
        if limit is not None and len(collected) >= limit:
            break
        locs = await _fetch_sitemap_locs(page, sitemap_url)
        for loc in locs:
            slug = product_slug(loc)
            if slug not in DESIGNER_SLUGS:
                continue
            url = rewrite_locale(loc, locale)
            if url and url not in seen:
                seen.add(url)
                collected.append(url)
        if idx < SITEMAP_SHARDS - 1:
            await asyncio.sleep(SITEMAP_DELAY_S)

    if limit is not None:
        collected = collected[:limit]
    return collected


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


async def db_counts() -> dict:
    collection = await get_sku_collection()
    return {
        "db_total_skus": await collection.count_documents({"shop": SHOP_ID}),
        "db_unique_urls": len(await collection.distinct("url", {"shop": SHOP_ID})),
        "db_in_stock": await collection.count_documents({"shop": SHOP_ID, "in_stock": True}),
        "db_out_stock": await collection.count_documents({"shop": SHOP_ID, "in_stock": False}),
    }


async def run_scraper(
    max_products: Optional[int] = MAX_PRODUCTS_DEFAULT,
    locale: str = LOCALE,
) -> dict:
    started = time.monotonic()
    print(f"SSENSE SCRAPER — starting (locale={locale}, currency={CURRENCY}, "
          f"max_products={max_products})", flush=True)

    stack = await asyncio.wait_for(_create_scrape_page(), timeout=LAUNCH_TIMEOUT_S)
    page = stack[3]

    stats: Dict[str, Any] = {
        "shop": SHOP_ID,
        "currency": CURRENCY,
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
            product_urls = await fetch_product_urls(page, limit=max_products, locale=locale)
        except Exception as e:  # noqa: BLE001
            print(f"FATAL: could not fetch catalogue: {e}", flush=True)
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
                    skus, reason = await asyncio.wait_for(
                        scrape_attempt(page, url), timeout=ATTEMPT_TIMEOUT_S
                    )
                except Exception as e:  # noqa: BLE001
                    reason = classify_failure(e)
                    skus = []
                    logger.warning("Attempt %d/%d failed for %s (%s); rebuilding browser",
                                   attempt, MAX_ATTEMPTS_PER_URL, url, reason)
                    stack = await _recreate_scrape_page(stack)
                    page = stack[3]
                    await asyncio.sleep(2)
                    continue

                # Success, permanent outcome, or a non-navigation failure
                if reason in ("", "http_404"):
                    break
                logger.warning("Attempt %d/%d: %s for %s", attempt, MAX_ATTEMPTS_PER_URL, reason, url)
                if attempt < MAX_ATTEMPTS_PER_URL:
                    stack = await _recreate_scrape_page(stack)
                    page = stack[3]
                    await asyncio.sleep(2 * attempt)

            stats["products_processed"] += 1
            title = skus[0].title if skus else url.rsplit("/", 2)[-2]

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
                print(f"--- progress {idx}/{total} | ok {stats['successful']} "
                      f"| failed {stats['failed']} | 404 {stats['not_found_404']} "
                      f"| new {stats['skus_inserted']} | updated {stats['skus_updated']} "
                      f"| elapsed {_fmt_duration(time.monotonic() - started)} ---", flush=True)

            await asyncio.sleep(REQUEST_DELAY_S)

        stats["failed_urls"] = failed_urls
        stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
        stats.update(await db_counts())

        print("", flush=True)
        print("=" * 60, flush=True)
        print("SSENSE SCRAPE RESULTS", flush=True)
        print("=" * 60, flush=True)
        print(f"Products discovered:      {stats['products_discovered']}", flush=True)
        print(f"Successful:               {stats['successful']}", flush=True)
        print(f"Failed:                   {stats['failed']}", flush=True)
        print(f"404 Not Found:            {stats['not_found_404']}", flush=True)
        print(f"New SKUs inserted:        {stats['skus_inserted']}", flush=True)
        print(f"Existing SKUs refreshed:  {stats['skus_updated']}", flush=True)
        print(f"SKU write failures:       {stats['skus_failed']}", flush=True)
        print(f"Total {SHOP_ID} SKUs in DB: {stats['db_total_skus']}", flush=True)
        print(f"Unique product URLs:      {stats['db_unique_urls']}", flush=True)
        print(f"In stock:                 {stats['db_in_stock']}", flush=True)
        print(f"Out of stock:             {stats['db_out_stock']}", flush=True)
        print(f"Runtime:                  {_fmt_duration(time.monotonic() - started)}", flush=True)
        print("=" * 60, flush=True)
        return stats
    finally:
        await _dispose(*stack)


async def probe() -> dict:
    """Read-only probe: confirm a browser session can reach SSENSE routes."""
    results: Dict[str, Any] = {"shop": SHOP_ID, "locale": LOCALE}
    stack = await _create_scrape_page()
    page = stack[3]
    try:
        for path in ("/", "/en-de/men", "/sitemap_products_list_0.xml"):
            try:
                response = await page.goto(BASE_URL + path, wait_until="domcontentloaded",
                                           timeout=NAV_TIMEOUT_MS)
                status = response.status if response else 0
                results[path] = {"status": status, "title": await page.title()}
            except Exception as e:  # noqa: BLE001
                results[path] = {"error": type(e).__name__}
        return results
    finally:
        await _dispose(*stack)


if __name__ == "__main__":
    import sys

    max_products = int(sys.argv[1]) if len(sys.argv) > 1 else MAX_PRODUCTS_DEFAULT
    result = asyncio.run(run_scraper(max_products))
    print(json.dumps({k: v for k, v in result.items() if k != "failed_urls"}, indent=2), flush=True)
