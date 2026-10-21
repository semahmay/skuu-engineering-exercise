from typing import Optional, List
from motor.motor_asyncio import AsyncIOMotorCollection
from app.models.sku import SKU, SKUInDB
from app.database.connection import get_database


COLLECTION_NAME = "skus"
RESTOCK_COLLECTION_NAME = "restock_events"


async def get_sku_collection() -> AsyncIOMotorCollection:
    db = get_database()
    return db[COLLECTION_NAME]


async def get_restock_collection() -> AsyncIOMotorCollection:
    """Append-only log of genuine out-of-stock -> in-stock transitions.

    One document per ``(shop, url, size)`` (unique) so a repeated refresh never
    duplicates an event. See ``app/refresh/restock.py``.
    """
    db = get_database()
    return db[RESTOCK_COLLECTION_NAME]


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
    restocks = await get_restock_collection()
    await restocks.create_index(
        [("shop", 1), ("url", 1), ("size", 1)],
        unique=True,
        name="uniq_restock_shop_url_size",
    )
    await restocks.create_index("last_detected_at")


async def insert_sku(sku: SKU) -> SKUInDB:
    collection = await get_sku_collection()
    doc = sku.model_dump()
    result = await collection.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    # The dashboard's matching snapshot must not outlive this write.
    from app.matching.service import invalidate_matching_cache

    invalidate_matching_cache(reason="insert_sku")
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
    if result.deleted_count:
        from app.matching.service import invalidate_matching_cache

        invalidate_matching_cache(reason="delete_sku_by_id")
    return result.deleted_count > 0