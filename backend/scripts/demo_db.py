"""Package / restore a portable demo snapshot of the SKUU database.

The dashboard needs a populated database so it can be demoed **without
re-scraping the storefronts**. This script writes one compressed archive that
contains every collection the application reads or writes:

    manifest.json        format + per-collection counts and index definitions
    skus.bson            the SKU / size-level inventory records
    restock_events.bson  genuine out-of-stock -> in-stock history

Each ``*.bson`` file is a stream of raw BSON documents (self-delimiting, so the
restore streams them without loading the whole collection into memory).

MongoDB **Database Tools** (``mongodump`` / ``mongorestore``) are *not*
required — this uses the official ``bson`` codec directly, so it works on a
machine that only has the Python backend dependencies.

Usage (from ``backend/``)::

    # export the live ``skuu`` database -> ../demo/skuu_demo.archive.gz
    python scripts/demo_db.py export

    # restore into a dedicated demo database (never the source ``skuu``)
    python scripts/demo_db.py import --db skuu_demo --drop

The export is strictly read-only against the source database. The import writes
only to ``--db`` and recreates the recorded indexes, including the unique
``(shop, url, size)`` indexes, so the restored collection behaves exactly like
the scraped one.
"""

import argparse
import io
import json
import struct
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from bson import BSON
from pymongo import MongoClient, ReplaceOne

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402

FORMAT = "skuu-demo-archive"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
ARCHIVE_NAME = "skuu_demo.archive.gz"
BATCH_SIZE = 1000

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARCHIVE = REPO_ROOT / "demo" / ARCHIVE_NAME

# Collections the application depends on (see app.database.repository).
DEFAULT_COLLECTIONS: Tuple[str, ...] = ("skus", "restock_events")

# Never let an accidental ``--drop`` wipe the production database.
PROTECTED_DATABASES = ("skuu",)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_indexes(collection) -> List[Dict]:
    """JSON-serialisable index definitions (``_id_`` is implicit)."""
    indexes: List[Dict] = []
    for name, spec in sorted(collection.index_information().items()):
        if name == "_id_":
            continue
        indexes.append(
            {
                "name": name,
                "key": [[field, int(direction)] for field, direction in spec["key"]],
                "unique": bool(spec.get("unique", False)),
            }
        )
    return indexes


def export_archive(
    uri: str,
    db_name: str,
    collections: Iterable[str],
    out_path: Path,
) -> Dict:
    """Write every present collection of ``db_name`` into a gzipped tar archive."""
    client = MongoClient(uri)
    db = client[db_name]
    present = set(db.list_collection_names())

    manifest: Dict = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "created_at": _now_iso(),
        "source_db": db_name,
        "collections": {},
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for name in collections:
            if name not in present:
                continue
            collection = db[name]
            # Stream to a temp file so the size is known before adding it to the
            # tar (tar headers need the length up front) without holding the
            # whole collection in memory.
            with io.BytesIO() as buffer:
                written = 0
                for doc in collection.find({}):
                    buffer.write(BSON.encode(doc))
                    written += 1
                payload = buffer.getvalue()

            info = tarfile.TarInfo(f"{name}.bson")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))

            manifest["collections"][name] = {
                "count": written,
                "indexes": _serialize_indexes(collection),
            }

        manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest_bytes)
        tar.addfile(info, io.BytesIO(manifest_bytes))

    client.close()
    return manifest


def _iter_bson(handle) -> Iterable[Dict]:
    """Yield documents from a stream of length-prefixed BSON documents."""
    while True:
        head = handle.read(4)
        if not head:
            return
        if len(head) < 4:
            raise ValueError("truncated BSON document header")
        (length,) = struct.unpack("<i", head)
        body = handle.read(length - 4)
        if len(body) != length - 4:
            raise ValueError("truncated BSON document body")
        yield BSON(head + body).decode()


def import_archive(
    uri: str,
    target_db: str,
    archive_path: Path,
    drop: bool = False,
) -> Tuple[Dict, Dict]:
    """Restore ``archive_path`` into ``target_db``, recreating indexes."""
    if not archive_path.exists():
        sys.exit(f"archive not found: {archive_path}")

    client = MongoClient(uri)
    db = client[target_db]
    report: Dict[str, Dict] = {}

    with tarfile.open(archive_path, "r:gz") as tar:
        manifest = json.load(tar.extractfile(MANIFEST_NAME))

        for name, meta in manifest["collections"].items():
            member = tar.extractfile(f"{name}.bson")
            if member is None:
                raise ValueError(f"archive is missing {name}.bson")
            collection = db[name]
            if drop:
                collection.drop()

            imported = 0
            batch: List[ReplaceOne] = []
            for doc in _iter_bson(member):
                batch.append(ReplaceOne({"_id": doc["_id"]}, doc, upsert=True))
                if len(batch) >= BATCH_SIZE:
                    collection.bulk_write(batch, ordered=False)
                    imported += len(batch)
                    batch = []
                    print(f"  {name}: imported {imported} ...", end="\r", flush=True)
            if batch:
                collection.bulk_write(batch, ordered=False)
                imported += len(batch)

            for index in meta.get("indexes", []):
                keys = [(field, direction) for field, direction in index["key"]]
                collection.create_index(
                    keys,
                    name=index["name"],
                    unique=bool(index.get("unique", False)),
                )

            report[name] = {
                "imported": imported,
                "count": collection.count_documents({}),
                "indexes": sorted(collection.index_information()),
            }

    client.close()
    return manifest, report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export/import a portable SKUU demo snapshot (gzipped BSON)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export_parser = sub.add_parser("export", help="write an archive from a database")
    export_parser.add_argument("output", nargs="?", default=str(DEFAULT_ARCHIVE))
    export_parser.add_argument("--db", default=settings.mongodb_db)
    export_parser.add_argument("--uri", default=settings.mongodb_uri)

    import_parser = sub.add_parser("import", help="restore an archive into a database")
    import_parser.add_argument("archive", nargs="?", default=str(DEFAULT_ARCHIVE))
    import_parser.add_argument("--db", default=settings.mongodb_db)
    import_parser.add_argument("--uri", default=settings.mongodb_uri)
    import_parser.add_argument("--drop", action="store_true", help="drop the collection first")
    import_parser.add_argument(
        "--force",
        action="store_true",
        help="allow --drop against a protected database name (e.g. skuu)",
    )

    args = parser.parse_args()

    if args.command == "export":
        out_path = Path(args.output)
        manifest = export_archive(
            args.uri, args.db, DEFAULT_COLLECTIONS, out_path
        )
        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"Exported from {args.db}:")
        for name, meta in manifest["collections"].items():
            print(f"  {name}: {meta['count']} documents, {len(meta['indexes'])} indexes")
        print(f"  -> {out_path} ({size_mb:.1f} MB)")
        return

    if args.db in PROTECTED_DATABASES and args.drop and not args.force:
        sys.exit(
            f"refusing to --drop the protected database {args.db!r}; "
            "restore into a dedicated demo database (e.g. --db skuu_demo) "
            "or pass --force if you really mean it"
        )

    archive_path = Path(args.archive)
    manifest, report = import_archive(
        args.uri, args.db, archive_path, drop=args.drop
    )
    print(f"Restored {archive_path} (from {manifest.get('source_db')}) into {args.db}:")
    for name, meta in report.items():
        print(
            f"  {name}: {meta['count']} documents, "
            f"indexes {meta['indexes']}"
        )


if __name__ == "__main__":
    main()
