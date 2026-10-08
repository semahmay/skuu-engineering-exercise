import asyncio
from app.database import get_database


async def check():
    db = get_database()
    docs = await db["skus"].find().to_list(1000)
    for d in docs:
        print(f'{d.get("shop")} | {d.get("title")} | {d.get("size")} | {d.get("price")} | {d.get("in_stock")}')
    print(f"Total: {len(docs)}")


asyncio.run(check())