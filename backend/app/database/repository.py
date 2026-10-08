from typing import Optional, List
from motor.motor_asyncio import AsyncIOMotorCollection
from app.models.sku import SKU, SKUInDB
from app.database.connection import get_database


COLLECTION_NAME = "skus"


async def get_sku_collection() -> AsyncIOMotorCollection:
    db = get_database()
    return db[COLLECTION_NAME]


async def create_indexes() -> None:
    collection = await get_sku_collection()
    await collection.create_index("shop")
    await collection.create_index("barcode")
    await collection.create_index([("brand", 1), ("title", 1), ("colour", 1), ("size", 1)])
    await collection.create_index("scraped_at")
    await collection.create_index([("shop", 1), ("in_stock", 1)])
    # Structural duplicate protection: one record per (shop, url, size)
    await collection.create_index(
        [("shop", 1), ("url", 1), ("size", 1)],
        unique=True,
        name="uniq_shop_url_size",
    )


async def insert_sku(sku: SKU) -> SKUInDB:
    collection = await get_sku_collection()
    doc = sku.model_dump()
    result = await collection.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return SKUInDB(**doc)


async def find_sku_by_id(sku_id: str) -> Optional[SKUInDB]:
    from bson import ObjectId
    collection = await get_sku_collection()
    doc = await collection.find_one({"_id": ObjectId(sku_id)})
    if doc:
        doc["_id"] = str(doc["_id"])
        return SKUInDB(**doc)
    return None


async def find_sku_by_shop_and_url(shop: str, url: str) -> Optional[SKUInDB]:
    collection = await get_sku_collection()
    doc = await collection.find_one({"shop": shop, "url": url})
    if doc:
        doc["_id"] = str(doc["_id"])
        return SKUInDB(**doc)
    return None


async def find_skus_by_shop(shop: str, limit: int = 100) -> List[SKUInDB]:
    collection = await get_sku_collection()
    cursor = collection.find({"shop": shop}).limit(limit)
    skus = []
    async for doc in cursor:
        doc["_id"] = str(doc["_id"])
        skus.append(SKUInDB(**doc))
    return skus


async def delete_sku_by_id(sku_id: str) -> bool:
    from bson import ObjectId
    collection = await get_sku_collection()
    result = await collection.delete_one({"_id": ObjectId(sku_id)})
    return result.deleted_count > 0