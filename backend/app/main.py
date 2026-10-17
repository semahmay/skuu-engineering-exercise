from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.api import api_router
from app.database.connection import close_connection


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_connection()


app = FastAPI(
    title="SKUU API",
    description="Inventory Intelligence API",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
async def root():
    return {"name": "SKUU API", "version": "0.1.0"}