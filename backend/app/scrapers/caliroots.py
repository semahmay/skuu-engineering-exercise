"""Caliroots retailer scraper (Shopify, SEK).

https://caliroots.com exposes ``/products.json`` and brand collections
``carhartt-wip`` / ``salomon`` / ``veja``. Prices are returned in SEK (the store
currency reported by ``/cart.js``). Sizes are already prefixed (``EU 40``,
``W27L32``) and are stored verbatim.
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

SHOP_ID = "caliroots"
RETAILER = ShopifyRetailer(
    shop_id=SHOP_ID,
    base_url="https://caliroots.com",
    currency="SEK",
    use_collections=True,
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
