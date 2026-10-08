import pytest
from datetime import datetime
from app.models.sku import SKU
from app.database import (
    check_connection,
    create_indexes,
    insert_sku,
    find_sku_by_id,
    find_sku_by_shop_and_url,
    delete_sku_by_id,
)


TEST_SKU = SKU(
    shop="test_shop",
    brand="TestBrand",
    title="Test Product",
    colour="Red",
    size="M",
    price=49.99,
    currency="EUR",
    barcode="9999999999999",
    in_stock=True,
    url="https://test-shop.com/test-product-red-m",
    scraped_at=datetime(2026, 10, 7, 12, 0, 0),
)


@pytest.mark.asyncio
async def test_mongodb_connection():
    connected = await check_connection()
    assert connected is True


@pytest.mark.asyncio
async def test_create_indexes():
    await create_indexes()


@pytest.mark.asyncio
async def test_insert_and_retrieve_sku():
    inserted = await insert_sku(TEST_SKU)
    assert inserted.id is not None
    assert inserted.shop == TEST_SKU.shop
    assert inserted.brand == TEST_SKU.brand
    assert inserted.title == TEST_SKU.title
    assert inserted.colour == TEST_SKU.colour
    assert inserted.size == TEST_SKU.size
    assert inserted.price == TEST_SKU.price
    assert inserted.currency == TEST_SKU.currency
    assert inserted.barcode == TEST_SKU.barcode
    assert inserted.in_stock == TEST_SKU.in_stock
    assert inserted.url == TEST_SKU.url
    assert inserted.scraped_at == TEST_SKU.scraped_at

    retrieved = await find_sku_by_id(inserted.id)
    assert retrieved is not None
    assert retrieved.id == inserted.id
    assert retrieved.shop == TEST_SKU.shop
    assert retrieved.brand == TEST_SKU.brand
    assert retrieved.title == TEST_SKU.title
    assert retrieved.colour == TEST_SKU.colour
    assert retrieved.size == TEST_SKU.size
    assert retrieved.price == TEST_SKU.price
    assert retrieved.currency == TEST_SKU.currency
    assert retrieved.barcode == TEST_SKU.barcode
    assert retrieved.in_stock == TEST_SKU.in_stock
    assert retrieved.url == TEST_SKU.url
    assert retrieved.scraped_at == TEST_SKU.scraped_at

    await delete_sku_by_id(inserted.id)
    deleted = await find_sku_by_id(inserted.id)
    assert deleted is None


@pytest.mark.asyncio
async def test_find_by_shop_and_url():
    inserted = await insert_sku(TEST_SKU)
    found = await find_sku_by_shop_and_url(TEST_SKU.shop, TEST_SKU.url)
    assert found is not None
    assert found.id == inserted.id
    await delete_sku_by_id(inserted.id)