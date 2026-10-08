import asyncio
from app.database import get_sku_collection


async def main():
    c = await get_sku_collection()
    dups = await c.aggregate([
        {"$match": {"shop": "carhartt_wip"}},
        {"$group": {"_id": {"shop": "$shop", "url": "$url", "size": "$size"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]).to_list(1000)
    print("duplicate groups:", len(dups))
    for d in dups[:20]:
        print(" ", d)

asyncio.run(main())
