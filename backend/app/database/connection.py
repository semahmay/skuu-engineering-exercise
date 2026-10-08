from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.config import settings


_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.mongodb_uri)
    return _client


def get_database() -> AsyncIOMotorDatabase:
    global _db
    if _db is None:
        client = get_client()
        _db = client[settings.mongodb_db]
    return _db


async def close_connection() -> None:
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db = None


async def check_connection() -> bool:
    try:
        client = get_client()
        await client.admin.command("ping")
        return True
    except Exception:
        return False