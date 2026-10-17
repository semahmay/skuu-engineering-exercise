"""Registry of shops refreshed by the "Refresh Inventory" pipeline.

Each entry points at an existing scraper module and the ``run_scraper`` call
that performs a *full* refresh. Modules are imported lazily (inside
:func:`load_scraper`) so importing this registry never pulls in Playwright or
other heavy dependencies — important for the test suite, which injects fake
scrapers instead.

``max_products=None`` means "no cap" for every scraper in this repo (Carhartt
and Veja treat ``None``/explicit caps via their own defaults; retailers and the
Shopify core iterate the whole catalogue).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.refresh.concurrency import SEQUENTIAL_ONLY_SHOPS, shop_limit


@dataclass(frozen=True)
class ShopSpec:
    shop_id: str
    label: str
    kind: str  # "brand" | "retailer"
    module: str
    kwargs: Dict[str, Any] = field(default_factory=dict)


# Order: brands first (own catalogues), then retailers. This is the order the
# pipeline scrapes and the order the dashboard lists shops in.
SHOPS: List[ShopSpec] = [
    ShopSpec("carhartt_wip", "Carhartt WIP", "brand", "app.scrapers.carhartt"),
    ShopSpec("veja", "VEJA", "brand", "app.scrapers.veja"),
    ShopSpec("salomon", "Salomon", "brand", "app.scrapers.salomon"),
    ShopSpec("asphaltgold", "Asphaltgold", "retailer", "app.scrapers.asphaltgold"),
    ShopSpec("afew", "Afew Store", "retailer", "app.scrapers.afew"),
    ShopSpec("beamhill", "Beamhill", "retailer", "app.scrapers.beamhill"),
    ShopSpec("biffi", "Biffi", "retailer", "app.scrapers.biffi"),
    ShopSpec("caliroots", "Caliroots", "retailer", "app.scrapers.caliroots"),
    ShopSpec("footway", "Footway", "retailer", "app.scrapers.footway"),
    ShopSpec("nugnes1920", "Nugnes 1920", "retailer", "app.scrapers.nugnes1920"),
    ShopSpec("sneakersnstuff", "Sneakersnstuff", "retailer", "app.scrapers.sneakersnstuff"),
    ShopSpec("ssense", "SSENSE", "retailer", "app.scrapers.ssense"),
    ShopSpec("xtreme", "Xtreme", "retailer", "app.scrapers.xtreme"),
]


def shop_ids() -> List[str]:
    return [s.shop_id for s in SHOPS]


def load_scraper(spec: ShopSpec) -> Callable[..., Any]:
    """Import the module for ``spec`` and return its ``run_scraper`` callable."""
    module = importlib.import_module(spec.module)
    return module.run_scraper


async def run_shop(
    spec: ShopSpec,
    *,
    concurrency: Optional[int] = None,
    gate: Optional[Any] = None,
) -> Dict[str, Any]:
    """Run one shop's full scraper and return its raw stats dict.

    ``concurrency``/``gate`` are forwarded only to scrapers that implement the
    shared bounded-concurrency policy. The Playwright per-product scrapers in
    :data:`SEQUENTIAL_ONLY_SHOPS` are always run sequentially (no extra kwargs),
    so they keep their existing single-page behaviour.
    """
    run_scraper = load_scraper(spec)
    kwargs = dict(spec.kwargs)
    if spec.shop_id not in SEQUENTIAL_ONLY_SHOPS:
        kwargs["concurrency"] = (
            concurrency if concurrency is not None else shop_limit(spec.shop_id)
        )
        kwargs["gate"] = gate
    return await run_scraper(max_products=None, **kwargs)


def stats_to_totals(stats: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """Normalise a scraper stats dict to ``(discovered, inserted, updated, failed)``.

    Scrapers share the ``skus_inserted``/``skus_updated``/``skus_failed`` keys
    and always report ``products_discovered``. Missing keys degrade to zero
    rather than raising so one malformed scraper response cannot abort the whole
    run.
    """
    return (
        int(stats.get("products_discovered", 0) or 0),
        int(stats.get("skus_inserted", 0) or 0),
        int(stats.get("skus_updated", 0) or 0),
        int(stats.get("skus_failed", 0) or 0),
    )


_FATAL_SENTINELS = frozenset({"sitemap", "catalogue", "products.json"})


def scraper_failed(stats: Dict[str, Any]) -> bool:
    """True when a scraper aborted before scraping any product.

    Carhartt/Salomon/VEJA/asphaltgold/SSENSE emit ``failed_urls`` entries named
    ``sitemap`` / ``products.json`` when the catalogue could not be fetched. A
    shop with zero discovered products and such a sentinel did not refresh
    successfully.
    """
    if int(stats.get("products_discovered", 0) or 0) > 0:
        return False
    for entry in stats.get("failed_urls", []) or []:
        url = entry[0] if isinstance(entry, (list, tuple)) and entry else entry
        if url in _FATAL_SENTINELS:
            return True
    return False
