"""Salomon official-store scraper (Shopify storefront, AU / NZ).

Why this source
---------------
The global site ``https://www.salomon.com`` is protected by PerimeterX / HUMAN
bot management: every product, category, GraphQL and REST route answers a 403
CAPTCHA challenge to automated clients (plain httpx *and* plain headless
Chromium). Defeating that protection is out of scope, so it is not touched.

Salomon's official regional shops ``salomon.com.au`` (AUD) and
``salomon.co.nz`` (NZD) run on Shopify and expose the public, unprotected
endpoint ``/products.json``. It returns the whole catalogue with, per variant:
``price``, ``available`` (the authoritative per-size stock flag), ``sku``,
``barcode`` and the option values (colour / size). That is the accessible public
source used here.

Stock is read verbatim from the Shopify variant ``available`` flag and is never
inferred from a product-level field.

Conventions (models, identity rule ``(shop, url, size)``, upsert) are reused
from the Carhartt/Veja scrapers without modifying them.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.models.sku import SKU
from app.database import get_sku_collection
from app.refresh.concurrency import RefreshConcurrency, clamp_limit, run_bounded
from app.scrapers.carhartt import classify_failure, upsert_skus

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SHOP_ID = "salomon"
BRAND = "Salomon"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Store:
    key: str
    base_url: str
    currency: str
    locale: str


STORES: Dict[str, Store] = {
    "au": Store("au", "https://salomon.com.au", "AUD", "en-AU"),
    "nz": Store("nz", "https://salomon.co.nz", "NZD", "en-NZ"),
}
DEFAULT_STORE = "au"

PAGE_LIMIT = 250          # Shopify /products.json maximum page size
MAX_PAGES = 60            # safety cap on pagination
DEFAULT_DELAY_MS = 400    # polite delay between pages
REQUEST_RETRIES = 3
REQUEST_TIMEOUT = 45
MAX_PRODUCTS_DEFAULT = 10
PROGRESS_EVERY = 100

SIZE_OPTION_NAMES = ("size", "shoe size")
COLOUR_OPTION_NAMES = ("color", "colour")
ONE_SIZE_LABEL = "One Size"


# ---------------------------------------------------------------------------
# Pure extraction helpers
# ---------------------------------------------------------------------------

def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def find_option_index(product: dict, names: Tuple[str, ...]) -> Optional[int]:
    """Index of the product option whose name matches any of ``names``.

    Match is case-insensitive and ignores surrounding whitespace.
    """
    wanted = {n.strip().lower() for n in names}
    for idx, option in enumerate(product.get("options") or []):
        name = str(option.get("name") or "").strip().lower()
        if name in wanted:
            return idx
    return None


def _variant_option(variant: dict, index: Optional[int]) -> str:
    """Value of ``option{index+1}`` for a variant (``""`` when absent)."""
    if index is None:
        return ""
    value = variant.get(f"option{index + 1}")
    if value is None:
        return ""
    return str(value).strip()


def extract_size(product: dict, variant: dict) -> str:
    """Displayed size; ``"One Size"`` when the product has no size option."""
    idx = find_option_index(product, SIZE_OPTION_NAMES)
    size = _variant_option(variant, idx)
    return size or ONE_SIZE_LABEL


def extract_colour(product: dict, variant: dict) -> str:
    """Colour option value (``""`` when the product has no colour option)."""
    idx = find_option_index(product, COLOUR_OPTION_NAMES)
    return _variant_option(variant, idx)


def variant_url(store: Store, handle: str, variant_id) -> str:
    """Canonical Shopify variant URL (falls back to product URL)."""
    base = f"{store.base_url}/products/{handle}"
    if variant_id in (None, ""):
        return base
    return f"{base}?variant={variant_id}"


def build_sku(
    product: dict,
    variant: dict,
    store: Store,
    scraped_at: Optional[datetime] = None,
) -> Optional[SKU]:
    """Build one SKU for a variant, or ``None`` when stock is unknown.

    ``available`` must be a real boolean; anything else is treated as unknown so
    stock is never guessed.
    """
    available = variant.get("available")
    if not isinstance(available, bool):
        return None

    handle = (product.get("handle") or "").strip()
    if not handle:
        return None

    return SKU(
        shop=SHOP_ID,
        brand=BRAND,
        title=(product.get("title") or "").strip(),
        colour=extract_colour(product, variant),
        size=extract_size(product, variant),
        price=_to_float(variant.get("price")),
        currency=store.currency,
        barcode=(variant.get("barcode") or None),
        in_stock=available,
        url=variant_url(store, handle, variant.get("id")),
        scraped_at=scraped_at or datetime.utcnow(),
    )


def parse_products(
    products: List[dict],
    store: Store,
    scraped_at: Optional[datetime] = None,
) -> Tuple[List[SKU], int]:
    """Build SKUs for a list of Shopify products.

    Returns ``(skus, skipped_variants)`` where ``skipped_variants`` counts
    variants whose stock could not be determined.
    """
    moment = scraped_at or datetime.utcnow()
    skus: List[SKU] = []
    skipped = 0
    for product in products:
        for variant in product.get("variants", []) or []:
            sku = build_sku(product, variant, store, moment)
            if sku is None:
                skipped += 1
            else:
                skus.append(sku)
    return skus, skipped


# ---------------------------------------------------------------------------
# Fetching (Shopify public /products.json)
# ---------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    )


async def _get_json(client: httpx.AsyncClient, url: str, retries: int = 3) -> Any:
    """GET a JSON endpoint with bounded retries; raises RuntimeError if exhausted."""
    last = "unknown"
    for attempt in range(1, retries + 1):
        try:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            last = f"http_{response.status_code}"
        except Exception as e:  # network, timeout, JSON error...
            last = classify_failure(e)
        if attempt < retries:
            await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"products.json fetch failed: {last} ({url})")


async def fetch_all_products(
    store: Store,
    limit: Optional[int] = None,
    max_pages: int = MAX_PAGES,
    delay_ms: int = DEFAULT_DELAY_MS,
) -> List[dict]:
    """Fetch the full public catalogue from ``/products.json`` with pagination."""
    products: List[dict] = []
    page = 1
    async with _client() as client:
        while page <= max_pages:
            url = f"{store.base_url}/products.json?limit={PAGE_LIMIT}&page={page}"
            payload = await _get_json(client, url)
            if not isinstance(payload, dict) or "products" not in payload:
                raise RuntimeError(f"unexpected products.json payload (page {page})")
            batch = payload.get("products") or []
            products.extend(batch)
            logger.info(
                "Fetched page %d: %d products (total %d)", page, len(batch), len(products)
            )
            if len(batch) < PAGE_LIMIT:
                break
            if limit and len(products) >= limit:
                break
            page += 1
            await asyncio.sleep(delay_ms / 1000)
    return products


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
        f"| failed {stats['failed']} | new {stats['skus_inserted']} "
        f"| updated {stats['skus_updated']} "
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
    max_products: Optional[int] = None,
    store_key: str = DEFAULT_STORE,
    *,
    concurrency: int = 1,
    gate: Optional[RefreshConcurrency] = None,
) -> dict:
    store = STORES.get(store_key)
    if store is None:
        raise ValueError(f"unknown store '{store_key}' (choose from {sorted(STORES)})")

    started = time.monotonic()
    print(
        f"SALOMON SCRAPE START | store={store.key} ({store.base_url}) "
        f"currency={store.currency} max_products={max_products}",
        flush=True,
    )

    stats = {
        "store": store.key,
        "currency": store.currency,
        "products_discovered": 0,
        "products_processed": 0,
        "successful": 0,
        "failed": 0,
        "variants_total": 0,
        "variants_skipped": 0,
        "skus_inserted": 0,
        "skus_updated": 0,
        "skus_failed": 0,
    }
    failed_products: List[Tuple[str, str]] = []

    try:
        products = await fetch_all_products(store, limit=max_products)
    except Exception as e:
        print(f"FATAL: could not fetch catalogue: {e}", flush=True)
        stats["failed_urls"] = [("products.json", classify_failure(e))]
        stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
        return stats

    if max_products is not None:
        products = products[:max_products]
    stats["products_discovered"] = len(products)
    total = len(products)
    print(f"Discovered {total} products", flush=True)

    async def _process(product: dict) -> Dict[str, Any]:
        variants = product.get("variants", []) or []
        skus: List[SKU] = []
        skipped = 0
        for variant in variants:
            sku = build_sku(product, variant, store)
            if sku is None:
                skipped += 1
            else:
                skus.append(sku)
        outcome: Dict[str, Any] = {
            "variants": len(variants),
            "skipped": skipped,
            "handle": product.get("handle", ""),
            "title": product.get("title", ""),
            "has_skus": bool(skus),
            "ok": True,
            "reason": "",
            "ins": 0,
            "upd": 0,
            "err": 0,
        }
        try:
            inserted, updated, failed = await upsert_skus(skus)
        except Exception as e:  # noqa: BLE001
            outcome["ok"] = False
            outcome["reason"] = classify_failure(e)
            return outcome
        outcome["ins"] = inserted
        outcome["upd"] = updated
        outcome["err"] = failed
        return outcome

    effective = clamp_limit(concurrency)
    if gate is not None:
        effective = min(effective, gate.global_limit)
    outcomes = await run_bounded(products, _process, limit=effective, rc=gate)

    for idx, outcome in enumerate(outcomes, 1):
        if outcome is None or not outcome[0]:
            stats["failed"] += 1
            reason = classify_failure(outcome[1]) if outcome else "unknown"
            failed_products.append(("", reason))
            print(f"[{idx}/{total}] — FAILED ({reason})", flush=True)
            continue
        result = outcome[1]
        stats["variants_total"] += result["variants"]
        stats["variants_skipped"] += result["skipped"]

        if not result["ok"]:
            stats["failed"] += 1
            failed_products.append((result["handle"], result["reason"]))
            print(f"[{idx}/{total}] {result['title'][:50]} — FAILED ({result['reason']})", flush=True)
            continue

        stats["skus_inserted"] += result["ins"]
        stats["skus_updated"] += result["upd"]
        stats["skus_failed"] += result["err"]
        stats["successful"] += 1
        stats["products_processed"] += 1

        note = "" if result["has_skus"] else " (no variants)"
        if result["skipped"]:
            note += f" [{result['skipped']} variant(s) with unknown stock skipped]"
        print(
            f"[{idx}/{total}] {result['title'][:50]} — "
            f"{result['ins']} new, {result['upd']} updated{note}",
            flush=True,
        )

        if idx % PROGRESS_EVERY == 0 or idx == total:
            _print_progress(idx, total, stats, started)

    elapsed = time.monotonic() - started
    stats["elapsed_seconds"] = round(elapsed, 1)
    stats["failed_urls"] = failed_products
    stats.update(await db_counts())

    print("", flush=True)
    print("=" * 60, flush=True)
    print("SALOMON SCRAPE RESULTS", flush=True)
    print("=" * 60, flush=True)
    print(f"Store:                   {store.key} ({store.base_url})", flush=True)
    print(f"Currency:                {store.currency}", flush=True)
    print(f"Products discovered:     {stats['products_discovered']}", flush=True)
    print(f"Products processed:      {stats['products_processed']}", flush=True)
    print(f"Successful:              {stats['successful']}", flush=True)
    print(f"Failed:                  {stats['failed']}", flush=True)
    print(f"Variants seen:           {stats['variants_total']}", flush=True)
    print(f"Variants (unknown stock):{stats['variants_skipped']}", flush=True)
    print(f"New SKUs inserted:       {stats['skus_inserted']}", flush=True)
    print(f"Existing SKUs refreshed: {stats['skus_updated']}", flush=True)
    print(f"SKU write failures:      {stats['skus_failed']}", flush=True)
    print(f"Total Salomon SKUs in DB:{stats['db_total_skus']}", flush=True)
    print(f"Unique product URLs:     {stats['db_unique_urls']}", flush=True)
    print(f"In stock:                {stats['db_in_stock']}", flush=True)
    print(f"Out of stock:            {stats['db_out_stock']}", flush=True)
    print(f"Runtime:                 {_fmt_duration(elapsed)}", flush=True)
    if failed_products:
        print("", flush=True)
        print(f"Failed products ({len(failed_products)}):", flush=True)
        for handle, reason in failed_products:
            print(f"  {handle} — {reason}", flush=True)
    print("=" * 60, flush=True)

    return stats


if __name__ == "__main__":
    import sys

    store_key = DEFAULT_STORE
    args = sys.argv[1:]
    if args and args[0].startswith("--store="):
        store_key = args[0].split("=", 1)[1]
        args = args[1:]
    max_products = int(args[0]) if args else MAX_PRODUCTS_DEFAULT
    if max_products > 50:
        logging.getLogger().setLevel(logging.WARNING)
    result = asyncio.run(run_scraper(max_products, store_key=store_key))
    print(json.dumps({k: v for k, v in result.items() if k != "failed_urls"}, indent=2), flush=True)
