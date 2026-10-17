from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class SKU(BaseModel):
    shop: str = Field(..., description="Shop identifier")
    brand: str = Field(..., description="Product brand")
    title: str = Field(..., description="Product title")
    colour: str = Field(..., description="Product colour")
    size: str = Field(..., description="Product size")
    price: float = Field(..., description="Product price")
    currency: str = Field(default="EUR", description="Currency code")
    barcode: Optional[str] = Field(default=None, description="Product barcode/EAN")
    style_code: Optional[str] = Field(
        default=None,
        description=(
            "Manufacturer style/variant code captured from the source (e.g. the "
            "Shopify variant SKU when it is not a GTIN). Stored for evidence and "
            "future use; it is deliberately NOT used as a matching identity."
        ),
    )
    in_stock: bool = Field(default=False, description="Stock availability")
    url: str = Field(..., description="Product URL")
    scraped_at: datetime = Field(default_factory=datetime.utcnow, description="Scraping timestamp")

    class Config:
        json_schema_extra = {
            "example": {
                "shop": "shop_a",
                "brand": "Nike",
                "title": "Air Max 270",
                "colour": "Black/White",
                "size": "42",
                "price": 129.99,
                "currency": "EUR",
                "barcode": "1234567890123",
                "style_code": "I032940_89",
                "in_stock": True,
                "url": "https://shop-a.com/nike-air-max-270-black-white-42",
                "scraped_at": "2026-10-07T12:00:00Z",
            }
        }


class SKUInDB(SKU):
    id: str = Field(..., alias="_id")

    class Config:
        populate_by_name = True