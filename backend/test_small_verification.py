import asyncio
import sys
import time
sys.path.insert(0, 'C:\\Users\\FMB SEMAH\\Desktop\\SKUU\\backend')

from app.scrapers.carhartt import scrape_product_page, create_browser_context, block_heavy_resources
from playwright.async_api import async_playwright

async def test_product(url, description):
    print(f"\n{'='*60}")
    print(f"TEST: {description}")
    print(f"URL: {url}")
    print(f"{'='*60}")
    
    start_time = time.time()
    
    playwright = await async_playwright().start()
    browser, context = await create_browser_context()
    page = await context.new_page()
    await block_heavy_resources(page)
    
    try:
        # DON'T navigate here - scrape_product_page does it
        skus = await scrape_product_page(page, url)
        
        elapsed = time.time() - start_time
        print(f"\nRuntime: {elapsed:.2f}s")
        print(f"Results: {len(skus)} SKUs")
        in_stock_count = sum(1 for s in skus if s.in_stock)
        out_of_stock_count = sum(1 for s in skus if not s.in_stock)
        print(f"  In stock: {in_stock_count}")
        print(f"  Out of stock: {out_of_stock_count}")
        
        for sku in skus:
            status = "IN STOCK" if sku.in_stock else "OUT OF STOCK"
            print(f"  {sku.size:>6} | {status}")
        
        return skus
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return []
        
    finally:
        await context.close()
        await browser.close()
        await playwright.stop()

async def main():
    # 3 test cases
    test_cases = [
        # 1. Mixed stock
        ("https://www.carhartt-wip.com/en-de/p/og-arcan-jacket-black-black-destroy-wash-303", 
         "OG Arcan Jacket - Mixed stock (XS sold out)"),
        
        # 2. One Size product
        ("https://www.carhartt-wip.com/en-de/p/kickflip-backpack-hamilton-brown-2101",
         "Kickflip Backpack - One Size"),
        
        # 3. Multi-size with mixed stock (real data)
        ("https://www.carhartt-wip.com/en-de/p/og-double-knee-pant-black-grind-washed-488",
         "OG Double Knee Pant - Mixed stock"),
    ]
    
    for url, desc in test_cases:
        await test_product(url, desc)
        await asyncio.sleep(2)

asyncio.run(main())