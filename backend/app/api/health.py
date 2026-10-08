from fastapi import APIRouter, Depends
from app.database.connection import check_connection


router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    db_ok = await check_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "disconnected",
    }