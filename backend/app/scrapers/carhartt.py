import asyncio
import logging
import re
import json
import time
from typing import List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from pydantic import ValidationError

from app.models.sku import SKU
from app.database import (
    insert_sku,
    find_sku_by_shop_and_url,
    get_sku_collection,
)
from app.refresh.concurrency import RefreshConcurrency, clamp_limit

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


SHOP_ID = "carhartt_wip"
BRAND = "Carhartt WIP"
BASE_URL = "https://www.carhartt-wip.com"
LOCALE = "en-de"
SITEMAP_URL = f"{BASE_URL}/{LOCALE}/product/sitemap.xml"

DEFAULT_DELAY_MS = 1500
SIZE_SELECT_DELAY_MS = 500
MAX_PRODUCTS_DEFAULT = 5
MAX_ATTEMPTS_PER_URL = 3   # bounded retry: 1 try + max 2 retries
PROGRESS_EVERY = 25

# Bounded product-level concurrency. Navigation latency (not extraction) dominates
# wall-clock time, so several product pages are processed in parallel. The default
# is deliberately conservative for retailer stability; the hard upper bound is a
# defensive cap so a misconfigured caller can never create an unbounded page/task
# fan-out. This changes *throughput*, never per-product extraction or identity.
MAX_CONCURRENT_PRODUCTS = 3
MAX_CONCURRENCY_LIMIT = 10

# Size radios are server-rendered, so they are present as soon as the document
# is parsed. We wait for the selector itself (a real readiness condition)
# instead of sleeping a fixed amount and hoping the page is ready.
SIZE_SELECTOR = 'input[name="intlSizes"], input[name="size"]'
READY_TIMEOUT_MS = 1000

# One in-page pass over the size radios. Extracting every radio's attributes in
# a single evaluate replaces ~6 CDP round trips per radio (the previous
# per-element locator calls), which dominated the per-product extraction cost.
_SIZE_EXTRACTION_JS = """
() => {
  let inputs = Array.from(document.querySelectorAll('input[name="intlSizes"]'));
  if (!inputs.length) {
    inputs = Array.from(document.querySelectorAll('input[name="size"]'));
  }
  return inputs.map((inp) => {
    const label = inp.parentElement ? inp.parentElement.parentElement : null;
    return {
      value: inp.getAttribute('value'),
      aria: inp.getAttribute('aria-label'),
      id: inp.getAttribute('id'),
      disabled: inp.disabled === true,
      label_class: label ? (label.getAttribute('class') || '') : '',
    };
  });
}
"""


@dataclass
class ProductData:
    title: str
    color: str
    price: float
    currency: str
    barcode: str
    url: str
    image_urls: List[str]
    material: Optional[str] = None


@dataclass
class SizeVariant:
    size: str
    variant_id: str  # The radio input ID (e.g., "I036867_25")
    in_stock: bool


async def create_browser_context() -> tuple[Browser, BrowserContext]:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
        locale="en-US",
    )
    return browser, context


async def block_heavy_resources(page: Page):
    async def _block(route):
        # ``continue_``/``abort`` can raise ``Invalid InterceptionId`` when the
        # page navigates again while a request from the previous document is
        # still in flight (we reuse one page across the whole catalogue, so this
        # happens at every navigation boundary). An unhandled error here leaves
        # the request un-resolved and destabilises later protocol calls, so it
        # must never propagate.
        try:
            if route.request.resource_type in ["image", "font", "media", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass
    await page.route("**/*", _block)


async def accept_cookies(page: Page) -> bool:
    selectors = [
        'button:has-text("Accept all")',
        'button:has-text("Accepter tout")',
        'button:has-text("Accepter")',
        'button:has-text("Accept")',
        '#onetrust-accept-btn-handler',
    ]
    for selector in selectors:
        try:
            btn = page.locator(selector).first
            if await btn.count() > 0:
                await btn.click()
                await page.wait_for_timeout(500)
                logger.debug("Accepted cookies via %s", selector)
                return True
        except Exception:
            continue
    return False


async def fetch_sitemap_urls(page: Page, limit: Optional[int] = None) -> List[str]:
    logger.info("Fetching sitemap: %s", SITEMAP_URL)
    response = await page.goto(SITEMAP_URL, wait_until="domcontentloaded", timeout=60000)
    if not response or response.status != 200:
        raise RuntimeError(f"Failed to fetch sitemap: {response.status if response else 'no response'}")

    content = await page.content()
    urls = re.findall(r"<loc>(https://www\.carhartt-wip\.com/" + re.escape(LOCALE) + r"/p/[^<]+)</loc>", content)
    logger.info("Found %d product URLs in sitemap", len(urls))

    if limit:
        urls = urls[:limit]
    return urls


async def parse_json_ld(page: Page) -> Optional[ProductData]:
    scripts = await page.locator('script[type="application/ld+json"]').all()
    for script in scripts:
        content = await script.inner_text()
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "@graph" in data:
                for item in data["@graph"]:
                    if item.get("@type") == "Product":
                        return _extract_product_data(item)
            elif isinstance(data, dict) and data.get("@type") == "Product":
                return _extract_product_data(data)
        except json.JSONDecodeError:
            continue
    return None


def _extract_product_data(item: dict) -> ProductData:
    offers = item.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    return ProductData(
        title=item.get("name", ""),
        color=item.get("color", ""),
        price=float(offers.get("price", 0)),
        currency=offers.get("priceCurrency", "EUR"),
        barcode=item.get("sku", ""),
        url=offers.get("url", ""),
        image_urls=item.get("image", []) if isinstance(item.get("image"), list) else [item.get("image", "")],
        material=item.get("material"),
    )


async def get_size_variants(page: Page) -> List[SizeVariant]:
    """
    Extract size variants from the DOM in a single in-page pass.

    Multi-size products: radio inputs have name="intlSizes", value=size,
    aria-label=size, disabled for sold-out. One Size products: radio input has
    name="size", value="0", disabled for sold-out. The label (the radio's
    grandparent) carries classes indicating availability.

    All radios are read in one ``page.evaluate`` so per-element CDP round trips
    (~6 per radio) are eliminated. The extracted values and stock rules are
    identical to the previous per-element implementation.
    """
    rows = await page.evaluate(_SIZE_EXTRACTION_JS)
    variants: List[SizeVariant] = []
    for row in rows:
        value = row.get("value")
        if not value:
            continue

        # Size label from aria-label (more reliable than value)
        size_label = row.get("aria") or value
        label_class = row.get("label_class") or ""

        # Determine stock status:
        # - Radio disabled attribute = sold out
        # - Label has line-through/text-midGrey/pointer-events-none = sold out
        is_sold_out = bool(row.get("disabled")) or (
            "line-through" in label_class
            or "text-midGrey" in label_class
            or "pointer-events-none" in label_class
        )

        variants.append(SizeVariant(
            size=size_label,
            variant_id=row.get("id") or "",
            in_stock=not is_sold_out,
        ))
        logger.debug("  Variant: size=%s, variant_id=%s, disabled=%s, label_class=%s, in_stock=%s",
                     size_label, row.get("id"), row.get("disabled"), label_class, not is_sold_out)

    return variants


async def scrape_product_page(page: Page, url: str) -> List[SKU]:
    """Single scrape attempt, returns SKUs (empty list on any failure)."""
    skus, _reason = await scrape_attempt(page, url)
    return skus


async def scrape_attempt(page: Page, url: str) -> Tuple[List[SKU], str]:
    """One attempt to scrape a product.

    Returns (skus, error_reason). error_reason == "" means the page was read
    successfully (skus may still be empty for a product with no sizes).
    Non-empty error_reason values: http_404, http_<code>, no_json_ld.
    Raises on navigation/timeout/crash errors (caller handles retries).
    """
    logger.info("Scraping product: %s", url)
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    status = response.status if response else 0
    if status == 404:
        logger.warning("404 Not Found: %s", url)
        return [], "http_404"
    if status != 200:
        logger.warning("Non-200 (%s): %s", status, url)
        return [], f"http_{status}"

    # Wait for the size radios to attach instead of sleeping a fixed second and
    # hoping the page is ready: navigation already waited for DOMContentLoaded,
    # so this returns almost immediately on the common server-rendered case and
    # still bounds the wait when a product renders no selector at all.
    try:
        await page.wait_for_selector(SIZE_SELECTOR, state="attached", timeout=READY_TIMEOUT_MS)
    except Exception:
        pass  # no size selector (e.g. fully sold out) -> JSON-LD fallback below
    await accept_cookies(page)

    product_data = await parse_json_ld(page)
    if not product_data:
        logger.error("No JSON-LD product data found for: %s", url)
        return [], "no_json_ld"

    # Get size variants from DOM (source of truth for availability)
    variants = await get_size_variants(page)
    logger.info("Found %d size variants on page", len(variants))

    skus = []
    
    if variants:
        # Normal case: size selector exists with variants
        for variant in variants:
            # For single-size products (accessories), use "One Size" if size is empty or numeric
            display_size = variant.size
            if not variant.size or variant.size.isdigit():
                display_size = "One Size"

            sku = SKU(
                shop=SHOP_ID,
                brand=BRAND,
                title=product_data.title,
                colour=product_data.color,
                size=display_size,
                price=product_data.price,
                currency=product_data.currency,
                barcode=product_data.barcode if product_data.barcode else None,
                in_stock=variant.in_stock,
                url=url,
                scraped_at=datetime.utcnow(),
            )
            skus.append(sku)
            logger.info("  SKU: %s | %s | %s | %.2f %s | stock=%s", SHOP_ID, product_data.title, display_size, product_data.price, product_data.currency, variant.in_stock)
    else:
        # Fully sold out: no size selector rendered
        # Check JSON-LD for sizes, but mark all as out of stock
        # We need to get sizes from somewhere - JSON-LD has them but they're all sold out
        scripts = await page.locator('script[type="application/ld+json"]').all()
        json_ld_sizes = []
        for script in scripts:
            content = await script.inner_text()
            try:
                data = json.loads(content)
                if isinstance(data, dict) and "@graph" in data:
                    for item in data["@graph"]:
                        if item.get("@type") == "Product":
                            json_ld_sizes = item.get("size", [])
                            break
                elif isinstance(data, dict) and data.get("@type") == "Product":
                    json_ld_sizes = data.get("size", [])
                    break
            except json.JSONDecodeError:
                continue
        
        if not isinstance(json_ld_sizes, list):
            json_ld_sizes = [str(json_ld_sizes)] if json_ld_sizes else []
        
        logger.warning("Product fully sold out (no size selector), using JSON-LD sizes (%d) with in_stock=False", len(json_ld_sizes))
        
        for size in json_ld_sizes:
            display_size = size
            if not size or str(size).isdigit():
                display_size = "One Size"
            
            sku = SKU(
                shop=SHOP_ID,
                brand=BRAND,
                title=product_data.title,
                colour=product_data.color,
                size=display_size,
                price=product_data.price,
                currency=product_data.currency,
                barcode=product_data.barcode if product_data.barcode else None,
                in_stock=False,  # Fully sold out
                url=url,
                scraped_at=datetime.utcnow(),
            )
            skus.append(sku)
            logger.info("  SKU: %s | %s | %s | %.2f %s | stock=False (sold out)", SHOP_ID, product_data.title, display_size, product_data.price, product_data.currency)

    return skus, ""


async def sku_exists(shop: str, url: str, size: str) -> bool:
    """Check if SKU with same shop, URL, and size already exists."""
    collection = await get_sku_collection()
    doc = await collection.find_one({"shop": shop, "url": url, "size": size})
    return doc is not None


async def save_skus(skus: List[SKU]) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    for sku in skus:
        try:
            if await sku_exists(sku.shop, sku.url, sku.size):
                logger.debug("SKU already exists, skipping: %s (%s)", sku.title, sku.size)
                skipped += 1
                continue
            await insert_sku(sku)
            inserted += 1
        except ValidationError as e:
            logger.error("Validation error for SKU %s: %s", sku.title, e)
        except Exception as e:
            logger.error("Error saving SKU %s: %s", sku.title, e)
    return inserted, skipped


async def upsert_skus(skus: List[SKU]) -> Tuple[int, int, int]:
    """Persist SKUs without ever creating duplicates.

    Identity = (shop, url, size). Each SKU is written with a single conditional
    upsert on that identity key: ``update_one(key, {"$set": doc}, upsert=True)``.
    The collection's unique ``(shop, url, size)`` index makes the match-or-insert
    atomic, so no ``find_one`` pre-read is needed. An existing row is refreshed
    in place (stock/price/title/scraped_at); a new row is inserted.

    Counts:
    - ``inserted`` — the upsert created a row (MongoDB reported ``upserted_id``).
    - ``updated`` — the upsert matched an existing row and refreshed it, or a
      concurrent insert raised ``DuplicateKeyError`` and the fallback plain
      ``update_one`` succeeded.
    - ``failed`` — a per-SKU error (validation or any other exception) was
      caught and logged; the remaining SKUs are still processed.

    Returns (inserted, updated, failed).
    """
    from pymongo.errors import DuplicateKeyError

    collection = await get_sku_collection()
    inserted = updated = failed = 0
    for sku in skus:
        key = {"shop": sku.shop, "url": sku.url, "size": sku.size}
        doc = sku.model_dump()
        try:
            # Single conditional upsert on the unique (shop, url, size) identity:
            # one round trip instead of a find_one pre-read plus a write. A new
            # row reports ``upserted_id`` (inserted); an existing row is matched
            # and refreshed in place (updated).
            result = await collection.update_one(key, {"$set": doc}, upsert=True)
            if getattr(result, "upserted_id", None) is not None:
                inserted += 1
            else:
                updated += 1
        except DuplicateKeyError:
            # Concurrent insert of the same identity -> refresh instead
            try:
                await collection.update_one(key, {"$set": doc})
                updated += 1
            except Exception as e:
                logger.error("Error updating SKU %s (%s): %s", sku.title, sku.size, e)
                failed += 1
        except ValidationError as e:
            logger.error("Validation error for SKU %s: %s", sku.title, e)
            failed += 1
        except Exception as e:
            logger.error("Error saving SKU %s (%s): %s", sku.title, sku.size, e)
            failed += 1
    return inserted, updated, failed


def classify_failure(exc: Exception) -> str:
    """Map an exception to a short, stable failure reason."""
    msg = str(exc).strip().splitlines()
    msg = msg[0][:150] if msg else ""
    low = msg.lower()
    if "crash" in low:
        return "page_crash"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "net::" in low:
        return "network_error"
    if "closed" in low:
        return "page_closed"
    return f"{exc.__class__.__name__}: {msg}" if msg else exc.__class__.__name__


async def _create_scrape_page():
    """Start a fresh Playwright + browser + page.

    Returns (playwright, browser, context, page).
    """
    playwright = await async_playwright().start()
    browser, context = await create_browser_context_with(playwright)
    page = await context.new_page()
    await block_heavy_resources(page)
    page.set_default_timeout(60000)
    return playwright, browser, context, page


async def create_browser_context_with(playwright) -> tuple[Browser, BrowserContext]:
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
        locale="en-US",
    )
    return browser, context


async def _recreate_scrape_page(old):
    """Dispose the old browser stack and start a fresh one, with bounded retries."""
    await _dispose(*old)
    last_exc = None
    for attempt in range(1, 4):
        try:
            return await _create_scrape_page()
        except Exception as e:  # browser failed to start
            last_exc = e
            logger.error("Browser restart attempt %d/3 failed: %s", attempt, e)
            await asyncio.sleep(5 * attempt)
    raise RuntimeError(f"could not restart browser: {last_exc}")


async def _recover_scrape_page(stack):
    """Restore a usable scrape page after a transient failure.

    Most failures are page-level (navigation timeout, page closed/crashed), not
    browser-level. Opening a fresh page in the existing context takes
    milliseconds, whereas relaunching Chromium takes seconds; so try the cheap
    path first and only fall back to a full rebuild when the browser stack is
    genuinely unusable. The bounded-retry policy is unchanged — this only makes
    each retry cheaper.
    """
    playwright, browser, context, page = stack
    try:
        if context is not None:
            if page is not None and not page.is_closed():
                await page.close()
            new_page = await context.new_page()
            await block_heavy_resources(new_page)
            new_page.set_default_timeout(60000)
            logger.debug("Recovered scrape page in existing browser")
            return playwright, browser, context, new_page
    except Exception as e:  # browser/context gone -> full rebuild
        logger.warning("In-place page recovery failed (%s); rebuilding browser", e)
    return await _recreate_scrape_page(stack)


async def _dispose(playwright=None, browser=None, context=None, page=None) -> None:
    # page dies with its context; closing context/browser/Playwright is enough
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


async def _close_page(page: Optional[Page]) -> None:
    """Close one product page, swallowing teardown errors (never leak a page)."""
    if page is None:
        return
    try:
        if not page.is_closed():
            await page.close()
    except Exception:
        pass


async def _sleep(seconds: float) -> None:
    """Indirection over ``asyncio.sleep`` so retry/politeness delays are patchable."""
    await asyncio.sleep(seconds)


class _ScrapeStack:
    """Owns the shared Chromium stack and hands out one isolated page per product.

    Concurrency model: every concurrent product task shares ONE browser and ONE
    context (a Chromium context can safely host many pages), but never shares a
    ``Page`` — each task opens its own page via :meth:`new_page` and closes it in
    a ``finally`` block, so no mutable page state crosses products.

    Page creation/recovery is serialised with a lock because the cheap path (a
    fresh page in the live context, milliseconds) and the expensive path
    (rebuilding Chromium when the context is genuinely dead) must not race each
    other. The lock is held only for that short operation, never during
    navigation, so it does not serialise the actual scraping.
    """

    def __init__(self, stack: Tuple) -> None:
        # (playwright, browser, context, None) — the page slot is intentionally
        # empty: pages are handed out per product, never kept here.
        self._stack = stack
        self._lock = asyncio.Lock()

    async def new_page(self) -> Page:
        """Return a fresh, isolated page from the shared browser/context."""
        async with self._lock:
            # ``_recover_scrape_page`` opens a new page in the existing context
            # (cheap) and only falls back to a full Chromium rebuild when the
            # context is unusable, preserving the existing bounded-restart policy.
            self._stack = await _recover_scrape_page(self._stack)
            playwright, browser, context, page = self._stack
            self._stack = (playwright, browser, context, None)
            return page

    async def close(self) -> None:
        """Tear down the browser stack (any still-open pages die with it)."""
        await _dispose(*self._stack)
        self._stack = (None, None, None, None)


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


async def _scrape_one_product(stack: "_ScrapeStack", url: str) -> Tuple[List[SKU], str]:
    """Scrape one product on its own isolated page, with the existing retries.

    Each attempt runs on a page owned by this task; a failed attempt closes the
    page and the next attempt opens a fresh one from the shared context (cheap —
    no unnecessary full Chromium rebuild). The page is closed in ``finally`` in
    every outcome, and the existing retry limit (``MAX_ATTEMPTS_PER_URL``) and
    classification semantics are unchanged.
    """
    skus: List[SKU] = []
    reason = ""
    page: Optional[Page] = None
    try:
        for attempt in range(1, MAX_ATTEMPTS_PER_URL + 1):
            try:
                if page is None or page.is_closed():
                    page = await stack.new_page()
                skus, reason = await scrape_attempt(page, url)
            except Exception as e:
                reason = classify_failure(e)
                skus = []
                logger.warning(
                    "Attempt %d/%d failed for %s (%s); retrying on a fresh page",
                    attempt, MAX_ATTEMPTS_PER_URL, url, reason,
                )
                await _close_page(page)
                page = None
                await _sleep(2)
                continue

            # Success, permanent 404, or retryable HTTP/render failure
            if reason in ("", "http_404"):
                break
            logger.warning(
                "Attempt %d/%d: %s for %s", attempt, MAX_ATTEMPTS_PER_URL, reason, url,
            )
            if attempt < MAX_ATTEMPTS_PER_URL:
                await _sleep(2 * attempt)
    finally:
        await _close_page(page)
    return skus, reason


async def run_scraper(
    max_products: int = MAX_PRODUCTS_DEFAULT,
    concurrency: int = MAX_CONCURRENT_PRODUCTS,
    *,
    gate: Optional[RefreshConcurrency] = None,
) -> dict:
    started = time.monotonic()
    # Bound concurrency explicitly (default 3), defensively (carhartt's own hard
    # cap) and by the shared refresh policy (per-shop ceiling of 5, and never
    # above the refresh-wide global limit). The semaphore is what actually
    # guarantees no unbounded page/task fan-out.
    concurrency = max(1, min(int(concurrency), MAX_CONCURRENCY_LIMIT))
    concurrency = clamp_limit(concurrency)
    if gate is not None:
        concurrency = min(concurrency, gate.global_limit)
    print(
        f"CARHARTT SCRAPER — starting (max_products={max_products}, concurrency={concurrency})",
        flush=True,
    )

    playwright, browser, context, page = await _create_scrape_page()
    # The initial page is only used to fetch the sitemap; every product gets its
    # own isolated page from the shared context (see ``_ScrapeStack``).
    stack = _ScrapeStack((playwright, browser, context, None))

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

    lock = asyncio.Lock()
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async def _handle(url: str, idx: int) -> None:
        nonlocal done
        try:
            skus, reason = await _scrape_one_product(stack, url)
        except Exception as e:  # belt-and-braces: one URL must never cancel others
            skus, reason = [], classify_failure(e)
            logger.exception("Unhandled error scraping %s", url)
        try:
            # Aggregate under a lock: counters and the failed-URL list are
            # shared, and the upsert is serialised here so concurrent products
            # never race on stats (and never issue concurrent DB writes).
            async with lock:
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
                    print(
                        f"[{idx}/{total}] {title} — {ins} new, {upd} updated{note}",
                        flush=True,
                    )

                done += 1
                if done % PROGRESS_EVERY == 0 or done == total:
                    _print_progress(done, total, stats, started)
        finally:
            # Per-product politeness delay (same cadence as the sequential
            # scraper). It sits inside the semaphore, so it also bounds how
            # quickly any one worker can issue its next navigation.
            await _sleep(DEFAULT_DELAY_MS / 1000)

    async def process(url: str, idx: int) -> None:
        # Per-shop semaphore, then (if configured) the shared refresh-wide slot,
        # so this shop can never push the whole refresh past the global ceiling.
        async with sem:
            if gate is None:
                await _handle(url, idx)
            else:
                async with gate.slot():
                    await _handle(url, idx)

    try:
        try:
            product_urls = await fetch_sitemap_urls(page, limit=max_products)
        except Exception as e:
            print(f"FATAL: could not fetch sitemap: {e}", flush=True)
            stats["failed_urls"] = [("sitemap", classify_failure(e))]
            return stats
        # Discovery is done; release the sitemap page and scrape concurrently.
        await _close_page(page)
        stats["products_discovered"] = len(product_urls)
        total = len(product_urls)
        print(f"Discovered {total} product URLs", flush=True)

        # A fixed pool of ``concurrency`` workers (never one task per URL) pulls
        # indices from a shared cursor, so a very large catalogue cannot spawn
        # thousands of tasks at once.
        cursor = {"i": 0}
        cursor_lock = asyncio.Lock()

        async def worker() -> None:
            while True:
                async with cursor_lock:
                    i = cursor["i"]
                    cursor["i"] += 1
                if i >= total:
                    return
                await process(product_urls[i], i + 1)

        await asyncio.gather(*(worker() for _ in range(min(concurrency, total))))
    finally:
        await stack.close()

    elapsed = time.monotonic() - started
    stats["elapsed_seconds"] = round(elapsed, 1)
    stats["failed_urls"] = failed_urls
    stats.update(await db_counts())

    print("", flush=True)
    print("=" * 60, flush=True)
    print("CARHARTT FULL SCRAPE RESULTS", flush=True)
    print("=" * 60, flush=True)
    print(f"Product URLs discovered: {stats['products_discovered']}", flush=True)
    print(f"Products processed:      {stats['products_processed']}", flush=True)
    print(f"Successful:              {stats['successful']}", flush=True)
    print(f"Failed:                  {stats['failed']}", flush=True)
    print(f"Skipped/404:             {stats['not_found_404']}", flush=True)
    print(f"Total SKUs in database:  {stats['db_total_skus']}", flush=True)
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


async def db_counts() -> dict:
    collection = await get_sku_collection()
    return {
        "db_total_skus": await collection.count_documents({"shop": SHOP_ID}),
        "db_unique_urls": len(await collection.distinct("url", {"shop": SHOP_ID})),
        "db_in_stock": await collection.count_documents({"shop": SHOP_ID, "in_stock": True}),
        "db_out_stock": await collection.count_documents({"shop": SHOP_ID, "in_stock": False}),
    }


if __name__ == "__main__":
    import sys
    max_products = int(sys.argv[1]) if len(sys.argv) > 1 else MAX_PRODUCTS_DEFAULT
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else MAX_CONCURRENT_PRODUCTS
    # Full runs only need progress lines + warnings/errors, not per-SKU chatter
    if max_products > 50:
        logging.getLogger().setLevel(logging.WARNING)
    result = asyncio.run(run_scraper(max_products, concurrency))
    print(json.dumps({k: v for k, v in result.items() if k != "failed_urls"}, indent=2), flush=True)