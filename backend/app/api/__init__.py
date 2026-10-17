from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.refresh import router as refresh_router

api_router = APIRouter()
api_router.include_router(health_router)
api_router.include_router(metrics_router)
api_router.include_router(refresh_router)
