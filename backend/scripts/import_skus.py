"""Restore a ``skus`` dump produced by ``scripts/export_skus.py``.

Usage (from ``backend/``)::

    python scripts/import_skus.py                       # reads data/skuu_skus.jsonl
    python scripts/import_skus.py out.jsonl
    python scripts/import_skus.py --db skuu --drop       # replace existing data

Documents are upserted by ``_id`` (idempotent, re-runnable) and the indexes from
``app.database.repository`` are (re)created, including the unique
``(shop, url, size)`` index, so the restored collection behaves exactly like the
scraped one.
"""

import argparse
import sys
from pathlib import Path

from bson import json_util
from pymongo import MongoClient, ReplaceOne

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

BATCH_SIZE = 1000


def _create_indexes(collection) -> None:
    collection.create_index("shop")
    collection.create_index("barcode")
    collection.create_index([("brand", 1), ("title", 1), ("colour", 1), ("size", 1)])
    collection.create_index("scraped_at")
    collection.create_index([("shop", 1), ("in_stock", 1)])
    collection.create_index(
        [("shop", 1), ("url", 1), ("size", 1)],
        unique=True,
        name="uniq_shop_url_size",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a skus JSONL dump.")
    parser.add_argument("input", nargs="?", default="data/skuu_skus.jsonl")
    parser.add_argument("--db", default=settings.mongodb_db)
    parser.add_argument("--collection", default="skus")
    parser.add_argument("--drop", action="store_true", help="drop the collection first")
    args = parser.parse_args()

    src = Path(args.input)
    if not src.exists():
        sys.exit(f"input not found: {src}")

    client = MongoClient(settings.mongodb_uri)
    collection = client[args.db][args.collection]
    if args.drop:
        collection.drop()

    imported = 0
    batch: list = []
    with src.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            doc = json_util.loads(line)
            batch.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
            if len(batch) >= BATCH_SIZE:
                collection.bulk_write(ops := batch, ordered=False)
                written = sum(r.upserted_id is not None or r.modified_count for r in ())
                written = len(ops)
                print(f"  imported {written} ...", end="\r", flush=True)
                batch = []

    if batch:
        collection.bulk_write(batch)

    _create_indexes(collection)
    total = collection.count_documents({})
    print(f"\nRestored {total} documents into {args.db}.{args.collection}")
    print("Indexes:", sorted(collection.index_information()))
    client.close()


if __name__ == "__main__":
    main()
