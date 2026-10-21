"""
Build the normalized vendor-spec index from product_attributes.

This is the hybrid-retrieval companion to promote_vendor_specs.py:

    product_attributes  -> product_vendor_specs   (all raw vendor attributes,
                                                   unit-normalized + canonical aliases)
    product_specs       -> taxonomy-promoted specs (authoritative filters)

Usage:
    python scripts/build_vendor_specs.py
    python scripts/build_vendor_specs.py --category motor,battery,microcontrollers
    python scripts/build_vendor_specs.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db
from src.spec.vendor_index import rebuild_vendor_specs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", default="", help="comma-separated categories")
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--db", default="", help="optional alternate SQLite path")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.db:
        db.settings.db_path = Path(args.db)
    categories = [c.strip() for c in args.category.split(",") if c.strip()] or None

    conn = db.get_db_connection()
    try:
        db.init_db()
        stats = rebuild_vendor_specs(
            conn,
            categories=categories,
            batch_size=max(1, args.batch_size),
            dry_run=args.dry_run,
        )
        logger.info(
            "Vendor spec index complete (dry_run=%s): %s",
            args.dry_run, stats,
        )
        if not args.dry_run:
            logger.info(
                "vendor rows=%s keys=%s",
                conn.execute("SELECT COUNT(1) FROM product_vendor_specs;").fetchone()[0],
                conn.execute("SELECT COUNT(1) FROM vendor_spec_stats_global;").fetchone()[0],
            )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
