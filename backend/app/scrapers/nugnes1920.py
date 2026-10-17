"""Nugnes 1920 retailer scraper (Shopify, EUR).

https://www.nugnes1920.com has no top-level brand collections (only
``carhartt-wip-man`` etc.), so it uses a full ``/products.json`` scan filtered by
brand vendor. The catalog is ~6.4k products (~26 pages).
"""

import asyncio
import json
import logging
import sys

from app.scrapers.shopify_retailer import (
    ShopifyRetailer,
    build_sku_for,
    db_counts_for,
    fetch_products_for,
    parse_products_for,
    run_scraper_for,
)

SHOP_ID = "nugnes1920"
RETAILER = ShopifyRetailer(
    shop_id=SHOP_ID,
    base_url="https://www.nugnes1920.com",
    currency="EUR",
    use_collections=False,
    full_scan=True,
)


def build_sku(product, variant, brand=None, scraped_at=None):
    return build_sku_for(RETAILER, product, variant, brand, scraped_at)


def parse_products(products, scraped_at=None):
    return parse_products_for(RETAILER, products, scraped_at)


async def fetch_products():
    return await fetch_products_for(RETAILER)


async def run_scraper(max_products=None, *, concurrency=1, gate=None):
    return await run_scraper_for(RETAILER, max_products, concurrency=concurrency, gate=gate)


async def db_counts():
    return await db_counts_for(RETAILER)


if __name__ == "__main__":
    args = sys.argv[1:]
    max_products = int(args[0]) if args else None
    if max_products and max_products > 50:
        logging.getLogger().setLevel(logging.WARNING)
    result = asyncio.run(run_scraper(max_products))
    print(json.dumps({k: v for k, v in result.items() if k != "failed_urls"}, indent=2))
