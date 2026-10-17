"""Export the ``skus`` collection to a portable extended-JSON file.

Usage (from ``backend/``)::

    python scripts/export_skus.py                  # -> data/skuu_skus.jsonl
    python scripts/export_skus.py out.jsonl
    python scripts/export_skus.py --db skuu --collection skus

The dump is MongoDB **extended JSON**, one document per line, so it preserves
ObjectIds, dates and numeric types exactly and can be re-imported with
``scripts/import_skus.py``. It lets the scraped demo dataset be shared without
re-running the scrapers (which hit live storefronts).
"""

import argparse
import sys
from pathlib import Path

from bson import json_util
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export the skus collection to newline-delimited extended JSON."
    )
    parser.add_argument("output", nargs="?", default="data/skuu_skus.jsonl")
    parser.add_argument("--db", default=settings.mongodb_db)
    parser.add_argument("--collection", default="skus")
    args = parser.parse_args()

    client = MongoClient(settings.mongodb_uri)
    collection = client[args.db][args.collection]
    total = collection.count_documents({})

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with out.open("w", encoding="utf-8") as handle:
        for doc in collection.find({}):
            handle.write(json_util.dumps(doc))
            handle.write("\n")
            written += 1

    size_mb = out.stat().st_size / (1024 * 1024)
    print(f"Exported {written}/{total} documents from {args.db}.{args.collection}")
    print(f"  -> {out} ({size_mb:.1f} MB)")
    client.close()


if __name__ == "__main__":
    main()
