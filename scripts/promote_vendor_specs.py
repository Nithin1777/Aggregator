"""
Promote vendor/title attributes into the taxonomy filter layer.

Run order:
    python scripts/backfill_specs.py          # definitions + spec_category + curated specs
    python scripts/promote_vendor_specs.py    # extension attributes into product_specs

Idempotent: rows with source='promoted' are replaced; curated 'rule' and
'manual' rows are never overwritten.

Usage:
    python scripts/promote_vendor_specs.py
    python scripts/promote_vendor_specs.py --category resistors,capacitors
    python scripts/promote_vendor_specs.py --limit 1000 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db
from scripts.backfill_specs import (
    recompute_spec_stats,
    recompute_spec_stats_global,
    sync_spec_definitions,
)
from src.spec.taxonomy_loader import load_taxonomy
from src.spec.vendor_specs import PROMOTIONS, build_promoted_specs

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _load_attributes(conn: sqlite3.Connection, categories: List[str]) -> Dict[int, Dict[str, Dict[str, Any]]]:
    placeholders = ", ".join(["?"] * len(categories))
    rows = conn.execute(
        f"SELECT product_id, key, value, value_num, unit, source, confidence "
        f"FROM product_attributes WHERE product_id IN ("
        f"SELECT id FROM products WHERE category_canonical IN ({placeholders}))",
        categories,
    ).fetchall()
    attrs: Dict[int, Dict[str, Dict[str, Any]]] = {}
    for product_id, key, value, value_num, unit, source, confidence in rows:
        attrs.setdefault(int(product_id), {})[str(key)] = {
            "key": key,
            "value": value,
            "value_num": value_num,
            "unit": unit,
            "source": source,
            "confidence": confidence,
        }
    return attrs


def promote(
    conn: sqlite3.Connection,
    categories: List[str],
    limit: int,
    dry_run: bool,
    batch_size: int,
) -> Dict[str, int]:
    taxonomy, _ = load_taxonomy(reload=True)
    stats = {"products": 0, "with_specs": 0, "rows": 0, "categories": len(categories)}
    attrs = _load_attributes(conn, categories)
    placeholders = ", ".join(["?"] * len(categories))
    rows = conn.execute(
        f"SELECT id, title, category_canonical, spec_category FROM products "
        f"WHERE category_canonical IN ({placeholders}) ORDER BY id;",
        categories,
    ).fetchall()
    if limit:
        rows = rows[:limit]
    stats["products"] = len(rows)
    logger.info("Promoting %s products across %s categories...", len(rows), len(categories))

    pending_specs: List[tuple] = []
    pending_category: List[tuple] = []
    pending_delete: List[tuple] = []
    committed = 0

    def flush() -> None:
        nonlocal committed
        if dry_run:
            pending_specs.clear()
            pending_category.clear()
            pending_delete.clear()
            return
        with conn:
            if pending_delete:
                conn.executemany(
                    "DELETE FROM product_specs WHERE product_id = ? AND source = 'promoted';",
                    pending_delete,
                )
            if pending_category:
                conn.executemany(
                    "UPDATE products SET spec_category = ? WHERE id = ? AND "
                    "(spec_category IS NULL OR spec_category = '');",
                    pending_category,
                )
            if pending_specs:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO product_specs
                        (product_id, key, value, value_num, value_type, confidence, evidence, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'promoted');
                    """,
                    pending_specs,
                )
        committed += 1
        pending_specs.clear()
        pending_category.clear()
        pending_delete.clear()

    for index, row in enumerate(rows, start=1):
        product_id = int(row[0])
        title = row[1] or ""
        category = row[2]
        if not category or category not in PROMOTIONS:
            continue
        specs = build_promoted_specs(title, category, attrs.get(product_id, {}), taxonomy)
        if not specs:
            continue
        stats["with_specs"] += 1
        stats["rows"] += len(specs)
        pending_delete.append((product_id,))
        pending_category.append((category, product_id))
        for spec in specs:
            pending_specs.append((
                product_id,
                spec["key"],
                spec["value"],
                spec["value_num"],
                spec["value_type"],
                spec["confidence"],
                spec["evidence"],
            ))
        if index % max(1, batch_size) == 0:
            flush()
            logger.info(
                "  scanned=%s with_specs=%s rows=%s",
                index, stats["with_specs"], stats["rows"],
            )

    flush()
    if not dry_run:
        logger.info("Recomputing spec definitions/stats...")
        sync_spec_definitions(conn, taxonomy)
        recompute_spec_stats(conn, taxonomy)
        recompute_spec_stats_global(conn)
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--category",
        default="",
        help="comma-separated categories; default all promoted categories",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--db", default="", help="optional alternate SQLite path (for a copy)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.db:
        db.settings.db_path = Path(args.db)

    taxonomy, _ = load_taxonomy(reload=True)
    available = [c for c in PROMOTIONS if c in taxonomy.categories]
    categories = (
        [c.strip() for c in args.category.split(",") if c.strip()]
        if args.category
        else available
    )
    unknown = [c for c in categories if c not in available]
    if unknown:
        logger.error("Unknown or non-promoted categories: %s", unknown)
        return 2

    conn = db.get_db_connection()
    try:
        db.init_db()
        stats = promote(
            conn,
            categories,
            max(0, args.limit),
            args.dry_run,
            max(1, args.batch_size),
        )
        logger.info("Promotion complete (dry_run=%s): %s", args.dry_run, stats)
        if not args.dry_run:
            spec_keys = conn.execute(
                "SELECT COUNT(1) FROM spec_stats_global;"
            ).fetchone()[0]
            logger.info("spec_stats_global keys: %s", spec_keys)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
