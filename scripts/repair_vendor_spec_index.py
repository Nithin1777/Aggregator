"""Back up the current catalog, then atomically rebuild its derived vendor index.

Source listings, attributes and promoted evidence are preserved. SQLite's backup
API captures a consistent snapshot even when the application uses WAL mode.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db
from config import settings
from src.spec.vendor_index import rebuild_vendor_specs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=Path("data/backups"))
    args = parser.parse_args()
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = args.backup_dir / f"components_before_vendor_repair_{timestamp}.db"
    conn = db.get_db_connection()
    try:
        with sqlite3.connect(backup_path) as backup:
            conn.backup(backup)
        print(json.dumps({"backup": str(backup_path.resolve()), "database": str(settings.db_path)}), flush=True)
        before = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        stats = rebuild_vendor_specs(conn)
        after = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if before != after:
            raise RuntimeError("Product count changed during repair; inspect concurrent ingestion.")
        print(json.dumps({"status": "repaired", "products": after, "index": stats}), flush=True)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
