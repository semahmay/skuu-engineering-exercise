import asyncio
import sys
sys.path.insert(0, 'C:\\Users\\FMB SEMAH\\Desktop\\SKUU\\backend')

from app.scrapers.carhartt import (
    scrape_product_page, create_browser_context, block_heavy_resources, save_skus,
)
from app.database import get_sku_collection

URL = "https://www.carhartt-wip.com/en-de/p/s-s-cardona-pocket-t-shirt-cardona-stripe-natural-dark-scarab-garment-dyed-131"

async def count_docs(collection, url):
    return await collection.count_documents({"shop": "carhartt_wip", "url": url})

async def main():
    collection = await get_sku_collection()

    # Start clean so run 1 is a genuine first insert
    deleted = await collection.delete_many({"shop": "carhartt_wip", "url": URL})
    print("CLEANUP: deleted", deleted.deleted_count, "existing records")
    print("BEFORE:", await count_docs(collection, URL), "records")

    playwright = None
    try:
        from playwright.async_api import async_playwright
        playwright = await async_playwright().start()
        browser, context = await create_browser_context()
        page = await context.new_page()
        await block_heavy_resources(page)

        # RUN 1
        skus = await scrape_product_page(page, URL)
        ins1, skp1 = await save_skus(skus)
        print(f"RUN 1: scraped={len(skus)}, inserted={ins1}, skipped={skp1}")
        print("AFTER RUN 1:", await count_docs(collection, URL), "records")

        # RUN 2 (same product, same session)
        skus2 = await scrape_product_page(page, URL)
        ins2, skp2 = await save_skus(skus2)
        print(f"RUN 2: scraped={len(skus2)}, inserted={ins2}, skipped={skp2}")
        print("AFTER RUN 2:", await count_docs(collection, URL), "records")

        # Verify uniqueness of shop+url+size
        pipeline = [
            {"$match": {"shop": "carhartt_wip", "url": URL}},
            {"$group": {"_id": "$size", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ]
        dups = await collection.aggregate(pipeline).to_list(100)
        print("DUPLICATE shop+url+size groups:", dups if dups else "none")
    finally:
        if playwright:
            await playwright.stop()

asyncio.run(main())
