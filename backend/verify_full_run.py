import asyncio
from app.database import get_sku_collection


async def main():
    c = await get_sku_collection()

    total = await c.count_documents({"shop": "carhartt_wip"})
    urls = await c.distinct("url", {"shop": "carhartt_wip"})
    in_stock = await c.count_documents({"shop": "carhartt_wip", "in_stock": True})
    out_stock = await c.count_documents({"shop": "carhartt_wip", "in_stock": False})
    titles = await c.distinct("title", {"shop": "carhartt_wip"})

    dup_groups = await c.aggregate([
        {"$match": {"shop": "carhartt_wip"}},
        {"$group": {"_id": {"shop": "$shop", "url": "$url", "size": "$size"},
                    "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]).to_list(1000)

    print("=" * 50)
    print("DATABASE VERIFICATION (shop = carhartt_wip)")
    print("=" * 50)
    print(f"Total SKUs:            {total}")
    print(f"Unique product URLs:   {len(urls)}")
    print(f"Unique product titles: {len(titles)}")
    print(f"In stock:              {in_stock}")
    print(f"Out of stock:          {out_stock}")
    print(f"Duplicate (shop,url,size) groups: {len(dup_groups)}")
    if dup_groups:
        for d in dup_groups[:10]:
            print("   DUP:", d)

    # sanity: any product with mixed stock?
    mixed = await c.aggregate([
        {"$match": {"shop": "carhartt_wip"}},
        {"$group": {"_id": "$url",
                    "total": {"$sum": 1},
                    "in_stock": {"$sum": {"$cond": ["$in_stock", 1, 0]}}}},
        {"$match": {"$expr": {"$and": [
            {"$gt": ["$in_stock", 0]},
            {"$gt": ["$total", "$in_stock"]},
        ]}}},
        {"$count": "n"},
    ]).to_list(1)
    print(f"Products with MIXED per-size stock: {mixed[0]['n'] if mixed else 0}")

    # indexes
    names = await c.index_information()
    print("Indexes:", list(names.keys()))

asyncio.run(main())
