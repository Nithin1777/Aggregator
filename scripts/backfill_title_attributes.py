"""
Backfill title-derived attributes without any network access.

Robu is behind Cloudflare, but its catalog titles are already in SQLite. This
extracts single-value electrical attributes ("10K 1/4W", "100uF 25V", "NEMA17
2A") into `product_attributes` so `get_product.vendor_specs` is useful for Robu
without a rescrape.

It is not a replacement for vendor description tables: values are heuristic
(source=title_heuristic, confidence 0.6), kits are skipped, and nothing here is
promoted into the taxonomy filter layer.

Usage:
    python scripts/backfill_title_attributes.py --vendors robu
    python scripts/backfill_title_attributes.py --vendors robu --limit 1000 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db
from src.spec.title_attributes import extract_title_attributes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def backfill(conn: sqlite3.Connection, vendor: str, limit: int, dry_run: bool) -> Dict[str, int]:
    stats = {"scanned": 0, "with_attributes": 0, "attributes": 0}
    rows = conn.execute(
        "SELECT id, title, category_canonical FROM products WHERE vendor = ? ORDER BY id;",
        (vendor,),
    ).fetchall()
    for product_id, title, category in rows:
        stats["scanned"] += 1
        if limit and stats["scanned"] > limit:
            break
        attrs = extract_title_attributes(title, category)
        if not attrs:
            continue
        stats["with_attributes"] += 1
        stats["attributes"] += len(attrs)
        if not dry_run:
            db.replace_product_attributes(
                int(product_id),
                attrs,
                source="title_heuristic",
                conn=conn,
                source_prefix="title",
            )
        if stats["scanned"] % 5000 == 0:
            logger.info(
                "[%s] scanned=%s with_attributes=%s attrs=%s",
                vendor, stats["scanned"], stats["with_attributes"], stats["attributes"],
            )
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendors", default="robu", help="comma-separated vendor keys")
    parser.add_argument("--limit", type=int, default=0, help="max products per vendor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    vendors = [v.strip() for v in args.vendors.split(",") if v.strip()]
    conn = db.get_db_connection()
    try:
        db.init_db()
        for vendor in vendors:
            stats = backfill(conn, vendor, max(0, args.limit), args.dry_run)
            logger.info(
                "%-18s scanned=%-7s with_attributes=%-7s attrs=%-7s",
                vendor, stats["scanned"], stats["with_attributes"], stats["attributes"],
            )
        if not args.dry_run:
            logger.info("Database totals: %s", db.description_stats())
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
