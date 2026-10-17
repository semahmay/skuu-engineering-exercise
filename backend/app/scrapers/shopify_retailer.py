"""Reusable core for Shopify-based multi-brand retailer scrapers.

Rationale
---------
The Shopify-based target retailers (all except SSENSE, which is Cloudflare-gated)
run on Shopify and expose the public, unprotected ``/products.json`` and
``/collections/<handle>/products.json`` endpoints. Each variant carries
``available`` (authoritative **per-size** stock), ``price``, ``barcode``/``sku``
and the option values. Rather than duplicate the ~350 lines for every shop, this
module holds the shared logic and each retailer module instantiates a
:class:`ShopifyRetailer` config and re-exports thin wrappers.

Scope
-----
Only products whose brand resolves to **Carhartt WIP / Salomon / Veja** are
ingested. Brand is taken from the Shopify ``vendor`` (case-insensitive), with the
product title as a fallback, and the source collection as a last-resort hint.

Honesty rules
-------------
- Stock is read verbatim from ``available``; a variant without a real bool is
  skipped, never guessed.
- Colour is only stored when the product exposes a colour option. Several of
  these retailers embed the colour in the title and expose no colour option, so
  their ``colour`` is left empty rather than parsed/invented.
- ``barcode`` comes from ``variant.barcode``; when that is empty, a GTIN-shaped
  ``variant.sku`` (all digits, length 8/12/13/14) is used, otherwise ``None``.
- ``style_code`` captures a non-GTIN ``variant.sku`` (a manufacturer style/variant
  code such as ``I032940_89``) verbatim for evidence. It is stored only and is
  deliberately **not** used as a matching identity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.models.sku import SKU
from app.database import get_sku_collection
from app.refresh.concurrency import RefreshConcurrency, clamp_limit, run_bounded
from app.scrapers.carhartt import classify_failure, upsert_skus

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

PAGE_LIMIT = 250
PAGE_DELAY_S = 0.4
REQUEST_TIMEOUT = 45
REQUEST_RETRIES = 3
PROGRESS_EVERY = 25

# Rate limiting (e.g. Biffi behind Cloudflare answers 429 + Retry-After).
RATE_LIMITED_STATUSES = (429, 503)
RATE_LIMIT_RETRIES = 4          # extra attempts while throttled
DEFAULT_RETRY_AFTER_S = 60.0    # fallback when the header is absent/invalid
MAX_RETRY_AFTER_S = 180.0       # cap a single wait so a bogus header can't hang us


class RateLimitedError(RuntimeError):
    """A catalogue request stayed rate-limited after honouring Retry-After.

    Raised instead of returning an empty result so a throttled collection is
    never silently mistaken for a missing/empty one.
    """


def _retry_after_seconds(header: Optional[str]) -> float:
    """Parse an HTTP ``Retry-After`` value (seconds or HTTP-date) to seconds."""
    if not header:
        return DEFAULT_RETRY_AFTER_S
    value = header.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return DEFAULT_RETRY_AFTER_S
        if when is None:
            return DEFAULT_RETRY_AFTER_S
        now = datetime.now(when.tzinfo) if when.tzinfo else datetime.now()
        seconds = (when - now).total_seconds()
    return max(0.0, min(seconds, MAX_RETRY_AFTER_S))

# Canonical brand => matched case-insensitively as a substring of vendor/title.
BRAND_TOKENS: Tuple[Tuple[str, str], ...] = (
    ("carhartt", "Carhartt WIP"),
    ("veja", "Veja"),
    ("salomon", "Salomon"),
)

# Collection handles tried for every collection-enabled retailer.
BRAND_COLLECTIONS: Tuple[Tuple[str, str], ...] = (
    ("Carhartt WIP", "carhartt-wip"),
    ("Salomon", "salomon"),
    ("Veja", "veja"),
)

SIZE_OPTION_NAMES = (
    "size",
    "sizes",
    "shoe size",
    "taglia",
    "talla",
    "taille",
    "maat",
    "tamanho",
    "grosse",
    "größe",
    "gross",
)
COLOUR_OPTION_NAMES = (
    "color",
    "colour",
    "colore",
    "cor",
    "couleur",
    "kleur",
    "farbe",
)
ONE_SIZE_LABEL = "One Size"


@dataclass(frozen=True)
class ShopifyRetailer:
    shop_id: str
    base_url: str
    currency: str
    use_collections: bool = True
    full_scan: bool = False
    max_pages: int = 120
    decimal_comma: bool = False
    request_delay_s: float = PAGE_DELAY_S
    strict_discovery: bool = False


# ---------------------------------------------------------------------------
# Pure extraction helpers
# ---------------------------------------------------------------------------

def detect_brand(*texts: Optional[str]) -> Optional[str]:
    """Canonical brand for the first text containing a brand token, else None."""
    for text in texts:
        low = (text or "").lower()
        for token, canonical in BRAND_TOKENS:
            if token in low:
                return canonical
    return None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def find_option_index(product: dict, names: Tuple[str, ...]) -> Optional[int]:
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
    size = _variant_option(variant, find_option_index(product, SIZE_OPTION_NAMES))
    return size or ONE_SIZE_LABEL


def _localize_size(size: str, decimal_comma: bool) -> str:
    """Rewrite a locale-specific decimal comma (``7,5`` -> ``7.5``).

    Some storefronts publish sizes with a comma decimal separator; the matching
    normalizer only keeps ``.`` as a decimal point, so a comma would collapse
    ``7,5`` to ``75``. Only retailers flagged ``decimal_comma`` are rewritten.
    """
    return size.replace(",", ".") if decimal_comma else size


def extract_colour(product: dict, variant: dict) -> str:
    return _variant_option(variant, find_option_index(product, COLOUR_OPTION_NAMES))


# Valid GTIN digit counts (GTIN-8/12/13/14 = EAN-8/UPC-A/EAN-13/GTIN-14).
GTIN_LENGTHS = frozenset((8, 12, 13, 14))


def _gtin_shaped(sku: str) -> Optional[str]:
    """Return ``sku`` only when it is a *bare* GTIN (all digits, GTIN length).

    Separators (``-``, spaces) are deliberately **not** removed first: stores
    routinely put a compound ``STYLE-SIZE`` / ``STYLE-COLOUR`` value in
    ``variant.sku`` (e.g. ``114805-37``), and collapsing the separator would
    fabricate an 8-digit "barcode" the source never published. Only a genuinely
    numeric code is treated as a GTIN; anything else is left for
    :func:`extract_variant_style_code`.
    """
    value = sku.strip()
    if value.isdigit() and len(value) in GTIN_LENGTHS:
        return value
    return None


def extract_barcode(variant: dict) -> Optional[str]:
    """EAN/UPC from ``barcode``; fall back to a *bare* GTIN-shaped ``sku``.

    The fallback never invents a barcode: a compound ``sku`` such as
    ``114805-37`` is a style+size code, not a GTIN, and yields ``None``.
    """
    barcode = str(variant.get("barcode") or "").strip()
    if barcode:
        return barcode
    return _gtin_shaped(str(variant.get("sku") or ""))


def extract_variant_style_code(variant: dict) -> Optional[str]:
    """Manufacturer style/variant code from ``variant.sku``, or ``None``.

    ``variant.sku`` holds either a real manufacturer style/variant code
    (``I032940_89``, ``L49306600-42``, ``114805-37``) or a GTIN-shaped
    brick-and-mortar code. A GTIN identifies a barcode, not a product style, so
    it is rejected here (it is already used as a ``barcode`` fallback). The
    value is captured verbatim and stored on the SKU as evidence; it is
    intentionally **not** used as a matching identity (see ``models/sku.py`` and
    ``matcher.py``).
    """
    sku = str(variant.get("sku") or "").strip()
    if not sku:
        return None
    if _gtin_shaped(sku):
        return None
    return sku


def variant_url(retailer: ShopifyRetailer, handle: str, variant_id: Any) -> str:
    base = f"{retailer.base_url}/products/{handle}"
    if variant_id in (None, ""):
        return base
    return f"{base}?variant={variant_id}"


def build_sku_for(
    retailer: ShopifyRetailer,
    product: dict,
    variant: dict,
    brand: Optional[str] = None,
    scraped_at: Optional[datetime] = None,
) -> Optional[SKU]:
    """One SKU for one variant, or ``None`` when stock/brand is unusable."""
    available = variant.get("available")
    if not isinstance(available, bool):
        return None
    handle = str(product.get("handle") or "").strip()
    if not handle:
        return None
    resolved = brand or detect_brand(product.get("vendor"), product.get("title"))
    if not resolved:
        return None
    return SKU(
        shop=retailer.shop_id,
        brand=resolved,
        title=str(product.get("title") or "").strip(),
        colour=extract_colour(product, variant),
        size=_localize_size(extract_size(product, variant), retailer.decimal_comma),
        price=_to_float(variant.get("price")),
        currency=retailer.currency,
        barcode=extract_barcode(variant),
        style_code=extract_variant_style_code(variant),
        in_stock=available,
        url=variant_url(retailer, handle, variant.get("id")),
        scraped_at=scraped_at or datetime.utcnow(),
    )


def parse_products_for(
    retailer: ShopifyRetailer,
    products: List[Tuple[dict, Optional[str]]],
    scraped_at: Optional[datetime] = None,
) -> Tuple[List[SKU], int]:
    """Build SKUs for ``[(product, brand_hint), ...]``.

    Returns ``(skus, skipped_variants)`` where ``skipped_variants`` counts
    variants with unknown stock (or unresolvable brand).
    """
    moment = scraped_at or datetime.utcnow()
    skus: List[SKU] = []
    skipped = 0
    for product, hint in products:
        for variant in product.get("variants", []) or []:
            sku = build_sku_for(retailer, product, variant, hint, moment)
            if sku is None:
                skipped += 1
            else:
                skus.append(sku)
    return skus, skipped


# ---------------------------------------------------------------------------
# Fetching (public Shopify JSON)
# ---------------------------------------------------------------------------

def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT,
    )


async def _get_json(client: httpx.AsyncClient, url: str, retries: int = REQUEST_RETRIES) -> Any:
    """GET JSON, honouring ``Retry-After`` on 429/503.

    Returns ``None`` when the endpoint genuinely does not exist (400/404) or
    after transient retries are exhausted, preserving the historic behaviour.
    Raises :class:`RateLimitedError` when a request stays throttled (429/503)
    after the retry budget — so a throttled collection is never silently
    mistaken for an empty one.
    """
    transient_attempts = 0
    rate_limit_attempts = 0
    last = "unknown"
    while True:
        try:
            response = await client.get(url)
            if response.status_code == 200:
                return response.json()
            last = f"http_{response.status_code}"
            # A 400/404 means the endpoint/collection does not exist — stop.
            if response.status_code in (400, 404):
                return None
            if response.status_code in RATE_LIMITED_STATUSES:
                rate_limit_attempts += 1
                if rate_limit_attempts > RATE_LIMIT_RETRIES:
                    raise RateLimitedError(
                        f"still {last} after {rate_limit_attempts} attempts: {url}"
                    )
                wait = _retry_after_seconds(response.headers.get("Retry-After"))
                logger.warning(
                    "Rate limited (%s); sleeping %.1fs before retry %d/%d: %s",
                    last, wait, rate_limit_attempts, RATE_LIMIT_RETRIES, url,
                )
                await asyncio.sleep(wait)
                continue
        except RateLimitedError:
            raise
        except Exception as e:  # noqa: BLE001
            last = classify_failure(e)

        transient_attempts += 1
        if transient_attempts >= retries:
            break
        await asyncio.sleep(2 * transient_attempts)

    logger.warning("GET failed (%s): %s", last, url)
    return None


async def _fetch_paginated(
    client: httpx.AsyncClient,
    url_template: str,
    max_pages: int,
    delay_s: float = PAGE_DELAY_S,
) -> List[dict]:
    """Fetch ``url_template.format(page=N)`` until a short page or cap.

    ``None`` (endpoint absent) ends pagination; a sustained rate limit
    propagates as :class:`RateLimitedError` so callers can retry or abort.
    """
    collected: List[dict] = []
    for page in range(1, max_pages + 1):
        data = await _get_json(client, url_template.format(page=page))
        if data is None:
            break
        batch = data.get("products") or []
        collected.extend(batch)
        if len(batch) < PAGE_LIMIT:
            break
        await asyncio.sleep(delay_s)
    return collected


async def fetch_products_for(retailer: ShopifyRetailer) -> List[Tuple[dict, Optional[str]]]:
    """Discover brand products as ``[(product, brand_hint), ...]``.

    Dedup is by product ``handle``. Brand collections are used when enabled;
    a full catalog scan runs when requested or when collections yield nothing
    (e.g. stores without a top-level brand collection).

    A collection that stays rate-limited raises :class:`RateLimitedError` for
    ``strict_discovery`` retailers (so a run aborts loudly instead of writing a
    partial catalogue); for others it is logged and skipped, as before.
    """
    seen: Dict[str, Tuple[dict, Optional[str]]] = {}
    async with _client() as client:
        if retailer.use_collections:
            for brand, handle in BRAND_COLLECTIONS:
                template = (
                    f"{retailer.base_url}/collections/{handle}"
                    "/products.json?limit=" + str(PAGE_LIMIT) + "&page={page}"
                )
                try:
                    products = await _fetch_paginated(
                        client, template, retailer.max_pages, retailer.request_delay_s
                    )
                except RateLimitedError as exc:
                    if retailer.strict_discovery:
                        raise
                    logger.error("Collection '%s' (%s) rate limited: %s", handle, brand, exc)
                    continue
                for product in products:
                    key = str(product.get("handle") or "")
                    if key and key not in seen:
                        seen[key] = (product, brand)
                await asyncio.sleep(retailer.request_delay_s)

        if retailer.full_scan or not seen:
            template = f"{retailer.base_url}/products.json?limit={PAGE_LIMIT}&page={{page}}"
            try:
                products = await _fetch_paginated(
                    client, template, retailer.max_pages, retailer.request_delay_s
                )
            except RateLimitedError as exc:
                if retailer.strict_discovery:
                    raise
                logger.error("Full-catalogue scan rate limited: %s", exc)
                products = []
            for product in products:
                key = str(product.get("handle") or "")
                if not key or key in seen:
                    continue
                brand = detect_brand(product.get("vendor"), product.get("title"))
                if brand:
                    seen[key] = (product, brand)

    resolved: List[Tuple[dict, Optional[str]]] = []
    for product, hint in seen.values():
        brand = detect_brand(product.get("vendor"), product.get("title")) or hint
        if brand:
            resolved.append((product, brand))
    return resolved


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


async def db_counts_for(retailer: ShopifyRetailer) -> dict:
    collection = await get_sku_collection()
    return {
        "db_total_skus": await collection.count_documents({"shop": retailer.shop_id}),
        "db_unique_urls": len(await collection.distinct("url", {"shop": retailer.shop_id})),
        "db_in_stock": await collection.count_documents({"shop": retailer.shop_id, "in_stock": True}),
        "db_out_stock": await collection.count_documents({"shop": retailer.shop_id, "in_stock": False}),
    }


async def run_scraper_for(
    retailer: ShopifyRetailer,
    max_products: Optional[int] = None,
    *,
    concurrency: int = 1,
    gate: Optional[RefreshConcurrency] = None,
) -> dict:
    """Scrape ``retailer``'s brand products.

    ``concurrency`` bounds how many products are processed at once (the pool is
    capped at ``min(concurrency, 5)``); ``gate`` is the shared refresh-wide
    :class:`RefreshConcurrency` gate that keeps every shop under one global
    ceiling. Product tasks only overlap **independent database upserts** on
    distinct ``(shop, url, size)`` keys — no extra request ever reaches the
    retailer — so raising this does not scrape any harder.
    """
    started = time.monotonic()
    print(
        f"{retailer.shop_id.upper()} SCRAPER | {retailer.base_url} "
        f"currency={retailer.currency} max_products={max_products}",
        flush=True,
    )

    stats: Dict[str, Any] = {
        "shop": retailer.shop_id,
        "currency": retailer.currency,
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
        products = await fetch_products_for(retailer)
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: could not fetch catalogue: {e}", flush=True)
        stats["failed_urls"] = [("catalogue", classify_failure(e))]
        stats["elapsed_seconds"] = round(time.monotonic() - started, 1)
        return stats

    if max_products is not None:
        products = products[:max_products]
    total = len(products)
    stats["products_discovered"] = total
    print(f"Discovered {total} brand products", flush=True)

    async def _process(item: Tuple[dict, str]) -> Dict[str, Any]:
        product, hint = item
        variants = product.get("variants", []) or []
        skus: List[SKU] = []
        skipped = 0
        for variant in variants:
            sku = build_sku_for(retailer, product, variant, hint)
            if sku is None:
                skipped += 1
            else:
                skus.append(sku)
        outcome: Dict[str, Any] = {
            "variants": len(variants),
            "skipped": skipped,
            "handle": product.get("handle", ""),
            "title": product.get("title", ""),
            "hint": hint,
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
            print(
                f"[{idx}/{total}] {result['title'][:50]} — FAILED ({result['reason']})",
                flush=True,
            )
            continue

        stats["skus_inserted"] += result["ins"]
        stats["skus_updated"] += result["upd"]
        stats["skus_failed"] += result["err"]
        stats["successful"] += 1
        stats["products_processed"] += 1

        note = "" if result["has_skus"] else " (no variants)"
        print(
            f"[{idx}/{total}] [{result['hint']}] {result['title'][:40]} "
            f"— {result['ins']} new, {result['upd']} updated{note}",
            flush=True,
        )

        if idx % PROGRESS_EVERY == 0 or idx == total:
            elapsed = time.monotonic() - started
            print(
                f"--- progress {idx}/{total} | ok {stats['successful']} "
                f"| failed {stats['failed']} | new {stats['skus_inserted']} "
                f"| updated {stats['skus_updated']} | {_fmt_duration(elapsed)} ---",
                flush=True,
            )

    elapsed = time.monotonic() - started
    stats["elapsed_seconds"] = round(elapsed, 1)
    stats["failed_urls"] = failed_products
    stats.update(await db_counts_for(retailer))

    print("", flush=True)
    print("=" * 60, flush=True)
    print(f"{retailer.shop_id.upper()} SCRAPE RESULTS", flush=True)
    print("=" * 60, flush=True)
    print(f"Products discovered:      {stats['products_discovered']}", flush=True)
    print(f"Successful:               {stats['successful']}", flush=True)
    print(f"Failed:                   {stats['failed']}", flush=True)
    print(f"Variants (unknown stock): {stats['variants_skipped']}", flush=True)
    print(f"New SKUs inserted:        {stats['skus_inserted']}", flush=True)
    print(f"Existing SKUs refreshed:  {stats['skus_updated']}", flush=True)
    print(f"SKU write failures:       {stats['skus_failed']}", flush=True)
    print(f"Total {retailer.shop_id} SKUs in DB: {stats['db_total_skus']}", flush=True)
    print(f"Unique product URLs:      {stats['db_unique_urls']}", flush=True)
    print(f"In stock:                 {stats['db_in_stock']}", flush=True)
    print(f"Out of stock:             {stats['db_out_stock']}", flush=True)
    print(f"Runtime:                  {_fmt_duration(elapsed)}", flush=True)
    print("=" * 60, flush=True)
    return stats
