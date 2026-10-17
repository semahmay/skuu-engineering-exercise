"""Phase definitions for the "Refresh Inventory" pipeline.

The order mirrors the exercise's pipeline and the work each phase performs:

1. ``initializing``   — load the configured shops and snapshot prior stock.
2. ``scraping``       — run the brand + retailer scrapers (each persists via
   the existing ``upsert_skus`` as it goes).
3. ``saving``         — surface the realised insert/update totals from the
   scrape (persistence itself happens inside each scraper; this phase reports
   it rather than performing a second write pass).
4. ``matching``       — recompute cross-shop matches with the existing engine.
5. ``back_in_stock``  — detect genuine out-of-stock -> in-stock transitions.
6. ``metrics``        — compute the dashboard metrics payload.

``completed`` / ``failed`` / ``cancelled`` are terminal states, not phases.
"""

from __future__ import annotations

from typing import List, Tuple

PHASE_ORDER: List[Tuple[str, str]] = [
    ("initializing", "Initializing refresh"),
    ("scraping", "Scraping brands and retailers"),
    ("saving", "Saving new and updated SKUs"),
    ("matching", "Matching products"),
    ("back_in_stock", "Checking back-in-stock opportunities"),
    ("metrics", "Updating dashboard metrics"),
]

PHASE_LABELS = dict(PHASE_ORDER)

TERMINAL_STATES = ("completed", "failed", "cancelled")

# Catalogue-level failure keys returned by the scrapers' ``failed_urls``: when
# one of these is present and nothing was processed, the shop could not be
# fetched and must NOT be reported as successfully refreshed.
CATALOGUE_FAILURE_KEYS = frozenset({"catalogue", "sitemap", "products.json"})
