"""AsphaltGold retailer scraper (Shopify storefront, EUR).

Why this retailer
-----------------
AsphaltGold (https://www.asphaltgold.com) is the first *retailer* added so the
same products can later be matched across shops. It was chosen because:

- it stocks **all three** of our existing brands (Salomon, Veja, Carhartt WIP);
- it is a Shopify storefront exposing the public, unprotected
  ``/products.json`` endpoint (no CAPTCHA / anti-bot gate);
- each variant carries ``available`` (authoritative **per-size** stock) and
  explicit ``Size`` + ``Color`` options (colour is not buried in the title);
- prices are consistent EUR.

We ingest **only the overlapping brands** and ignore the rest of the catalog.

Stock is read verbatim from the Shopify variant ``available`` flag and is never
inferred. Reuses ``upsert_skus`` / ``classify_failure`` from ``carhartt.py``
without modifying it.
"""

import asyncio
import json
import logging
import time
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

SHOP_ID = "asphaltgold"
BASE_URL = "https://www.asphaltgold.com"
CURRENCY = "EUR"

# Only ingest products whose Shopify ``vendor`` is one of our existing brands.
OVERLAP_BRANDS = ("Salomon", "Veja", "Carhartt WIP")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

PAGE_LIMIT = 250          # Shopify /products.json maximum page size
MAX_PAGES = 80            # safety cap on pagination
PAGE_DELAY_S = 0.4        # polite delay between pages
PROGRESS_EVERY = 25

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
    """Index of the option whose name matches any of ``names`` (case-insensitive)."""
    wanted = {n.strip().lower() for n in names}
    for idx, option in enumerate(product.get("options") or []):
        if str(option.get("name") or "").strip().lower() in wanted:
            return idx
    return None


def _variant_option(variant: dict, index: Optional[int]) -> str:
    if index is None:
        return ""
    value = variant.get(f"option{index + 1}")
    return "" if value is None else str(value).strip()


def extract_size(product: dict, variant: dict) -> str:
    """Displayed size; ``"One Size"`` when the product has no size option."""
    size = _variant_option(variant, find_option_index(product, SIZE_OPTION_NAMES))
    return size or ONE_SIZE_LABEL


def extract_colour(product: dict, variant: dict) -> str:
    """Displayed colour (``""`` when the product has no colour option)."""
    return _variant_option(variant, find_option_index(product, COLOUR_OPTION_NAMES))


def is_overlap_brand(product: dict) -> bool:
    return str(product.get("vendor") or "").strip() in OVERLAP_BRANDS


def variant_url(handle: str, variant_id: Any) -> str:
    base = f"{BASE_URL}/products/{handle}"
    if variant_id in (None, ""):
        return base
    return f"{base}?variant={variant_id}"


def build_sku(product: dict, variant: dict, scraped_at: Optional[datetime] = None) -> Optional[SKU]:
    """One SKU for one (colour, size) variant.

    Returns ``None`` when the variant has no reliable per-size stock (i.e.
    ``available`` is not a real bool), so stock is never guessed.
    """
    available = variant.get("available")
    if not isinstance(available, bool):
        return None
    handle = str(product.get("handle") or "").strip()
    if not handle:
        return None

    return SKU(
        shop=SHOP_ID,
        brand=str(product.get("vendor") or "").strip(),
        title=str(product.get("title") or "").strip(),
        colour=extract_colour(product, variant),
        size=extract_size(product, variant),
        price=_to_float(variant.get("price")),
        currency=CURRENCY,
        barcode=(variant.get("barcode") or None),
        in_stock=available,
        url=variant_url(handle, variant.get("id")),
        scraped_at=scraped_at or datetime.utcnow(),
    )


def parse_products(products: List[dict], scraped_at: Optional[datetime] = None) -> Tuple[List[SKU], int]:
    """Build SKUs for a list of Shopify products.

    Returns ``(skus, skipped_variants)`` where ``skipped_variants`` counts
    variants without a reliable stock state.
    """
    moment = scraped_at or datetime.utcnow()
    skus: List[SKU] = []
    skipped = 0
    for product in products:
        for variant in product.get("variants", []) or []:
            sku = build_sku(product, variant, moment)
            if sku is None:
                skipped += 1
            else:
                skus.append(sku)
    return skus, skipped


# ---------------------------------------------------------------------------
# Catalogue discovery (public /products.json, brand-filtered)
# ---------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        timeout=45,
    )


async def _get_json(client: httpx.AsyncClient, url: str, retries: int = 3) -> Any:
    last = "unknown"
    for attempt in range(1, retries + 1):
        try:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            last = f"http_{response.status_code}"
        except Exception as e:
            last = classify_failure(e)
        if attempt < retries:
            await asyncio.sleep(2 * attempt)
    raise RuntimeError(f"products.json fetch failed: {last} ({url})")


async def fetch_products() -> List[dict]:
    """Fetch and filter the whole catalog to the overlapping brands.

    Paginates ``/products.json?limit=250&page=N`` until a short page. Only
    products whose ``vendor`` is in :data:`OVERLAP_BRANDS` are kept.
    """
    selected: List[dict] = []
    async with _client() as client:
        for page in range(1, MAX_PAGES + 1):
            data = await _get_json(client, f"{BASE_URL}/products.json?limit={PAGE_LIMIT}&page={page}")
            batch = data.get("products", [])
            total = len(batch)
            for p in batch:
                if is_overlap_brand(p):
                    selected.append(p)
            print(f"  page {page}: {total} products, {len(selected)} matching so far", flush=True)
            if total < PAGE_LIMIT:
                break
            await asyncio.sleep(PAGE_DELAY_S)
    return selected


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
    max_products: Optional[int] = None,
    *,
    concurrency: int = 1,
    gate: Optional[RefreshConcurrency] = None,
) -> dict:
    """Scrape AsphaltGold brand products.

    ``concurrency`` bounds how many products are processed at once (capped at 5);
    ``gate`` is the shared refresh-wide gate. Product tasks only overlap
    independent database upserts on distinct keys — no extra request reaches the
    retailer — so raising ``concurrency`` does not scrape any harder.
    """
    started = time.monotonic()
    print(f"ASPHALTGOLD SCRAPER — overlapping brands={OVERLAP_BRANDS} max_products={max_products}", flush=True)

    stats: Dict[str, Any] = {
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
        products = await fetch_products()
    except Exception as e:
        print(f"FATAL: could not fetch catalogue: {e}", flush=True)
        stats["failed_urls"] = [("products.json", classify_failure(e))]
        stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
        return stats

    if max_products is not None:
        products = products[:max_products]
    stats["products_discovered"] = len(products)
    total = len(products)
    print(f"Discovered {total} overlapping-brand products", flush=True)

    async def _process(product: dict) -> Dict[str, Any]:
        variants = product.get("variants", []) or []
        skus: List[SKU] = []
        skipped = 0
        for variant in variants:
            sku = build_sku(product, variant)
            if sku is None:
                skipped += 1
            else:
                skus.append(sku)
        outcome: Dict[str, Any] = {
            "variants": len(variants),
            "skipped": skipped,
            "handle": product.get("handle", ""),
            "title": product.get("title", ""),
            "vendor": product.get("vendor"),
            "has_skus": bool(skus),
            "ok": True,
            "reason": "",
            "ins": 0,
            "upd": 0,
            "err": 0,
        }
        try:
            ins, upd, err = await upsert_skus(skus)
        except Exception as e:  # noqa: BLE001
            outcome["ok"] = False
            outcome["reason"] = classify_failure(e)
            return outcome
        outcome["ins"] = ins
        outcome["upd"] = upd
        outcome["err"] = err
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
            note += f" [{result['skipped']} variant(s) without stock skipped]"
        print(f"[{idx}/{total}] [{result['vendor']}] {result['title'][:45]} — {result['ins']} new, {result['upd']} updated{note}", flush=True)

        if idx % 25 == 0 or idx == total:
            elapsed = time.monotonic() - started
            print(f"--- progress {idx}/{total} | ok {stats['successful']} | failed {stats['failed']} "
                  f"| new {stats['skus_inserted']} | updated {stats['skus_updated']} | {_fmt_duration(elapsed)} ---", flush=True)

    elapsed = time.monotonic() - started
    stats["elapsed_seconds"] = round(elapsed, 1)
    stats["failed_urls"] = failed_products
    stats.update(await db_counts())

    print("", flush=True)
    print("=" * 60, flush=True)
    print("ASPHALTGOLD SCRAPE RESULTS", flush=True)
    print("=" * 60, flush=True)
    print(f"Overlapping brands:      {OVERLAP_BRANDS}", flush=True)
    print(f"Products discovered:     {stats['products_discovered']}", flush=True)
    print(f"Products processed:      {stats['products_processed']}", flush=True)
    print(f"Successful:              {stats['successful']}", flush=True)
    print(f"Failed:                  {stats['failed']}", flush=True)
    print(f"Variants seen:           {stats['variants_total']}", flush=True)
    print(f"Variants (unknown stock):{stats['variants_skipped']}", flush=True)
    print(f"New SKUs inserted:       {stats['skus_inserted']}", flush=True)
    print(f"Existing SKUs refreshed: {stats['skus_updated']}", flush=True)
    print(f"SKU write failures:      {stats['skus_failed']}", flush=True)
    print(f"Total AsphaltGold SKUs:  {stats['db_total_skus']}", flush=True)
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

    args = sys.argv[1:]
    max_products = int(args[0]) if args else 10
    if max_products > 50:
        logging.getLogger().setLevel(logging.WARNING)
    result = asyncio.run(run_scraper(max_products))
    print(json.dumps({k: v for k, v in result.items() if k != "failed_urls"}, indent=2), flush=True)
