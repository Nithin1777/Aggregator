"""
Complete the Robu catalog through ProductDetails GraphQL instead of the
Cloudflare-blocked search API.

The sitemap registry still contains pending slugs. For each batch of slugs this
script:
  1. fetches ProductDetails via GraphQL (aliased batch),
  2. upserts a UnifiedProduct,
  3. stores description + parsed attributes,
  4. marks the registry slug indexed.

It is idempotent and resumable: pending slugs are read fresh on every run, and
already indexed slugs are never fetched again.

Usage:
    python scripts/ingest_robu_graphql.py --limit 100 --batch-size 25
    python scripts/ingest_robu_graphql.py --dry-run --limit 10
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db
from adapters.robu_graphql import (
    RobuGraphQLError,
    RobuGraphQLClient,
    description_html,
)
from models import UnifiedProduct
from normalizer import extract_mpn, normalize_title
from src.spec.description import description_text, extract_description_attributes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _product_from_detail(slug: str, detail: Dict[str, Any]) -> UnifiedProduct:
    name = str(detail.get("name") or "").strip()
    url_key = str(detail.get("url_key") or slug).strip()
    categories = detail.get("categories") or []
    category = str(categories[0].get("name")) if categories else None
    images = detail.get("images") or []
    image_url = str(images[0].get("url")) if images else None
    price_block = detail.get("price") or {}
    price = price_block.get("final") or price_block.get("regular") or 0.0
    try:
        price = float(price)
    except (TypeError, ValueError):
        price = 0.0

    return UnifiedProduct(
        vendor="robu",
        vendor_name="Robu.in",
        external_id=str(detail.get("id") or detail.get("sku") or slug),
        title=name,
        normalized_title=normalize_title(name),
        mpn=extract_mpn(name),
        sku=detail.get("sku"),
        price=price,
        available=bool(detail.get("in_stock")),
        product_url=f"https://robu.in/product/{url_key}/",
        image_url=image_url,
        category=category,
        price_note=None,
        extra={
            "slug": url_key,
            "categories": categories,
            "source": "robu_graphql",
        },
    )


def _persist(conn: sqlite3.Connection, product_id: int, html: str, dry_run: bool) -> int:
    attrs = extract_description_attributes(html)
    text = description_text(html)
    if dry_run:
        return len(attrs)
    db.save_product_description(product_id, text, source="robu_graphql", conn=conn)
    return db.replace_product_attributes(
        product_id,
        attrs,
        source="description",
        conn=conn,
        source_prefix="description",
    )


def run(
    limit: int,
    batch_size: int,
    sleep_s: float,
    dry_run: bool,
) -> Dict[str, int]:
    stats = {"batches": 0, "slugs": 0, "upserted": 0, "described": 0, "attributes": 0, "missing": 0}
    client = RobuGraphQLClient()
    conn = db.get_db_connection()
    try:
        while True:
            remaining = limit - stats["slugs"] if limit else batch_size
            if limit and remaining <= 0:
                break
            pending = db.get_pending_robu_slugs(limit=min(batch_size, remaining) if limit else batch_size)
            if not pending:
                logger.info("No pending Robu slugs left.")
                break

            slugs = [item["slug"] for item in pending]
            try:
                details = client.fetch_batch(slugs, batch_size=batch_size)
            except RobuGraphQLError as exc:
                logger.warning("Stopped at %s pending slugs: %s", len(slugs), exc)
                break
            if not details:
                logger.warning("No product details returned; stopping this batch.")
                break

            for slug, detail in details.items():
                stats["slugs"] += 1
                product = _product_from_detail(slug, detail)
                if not product.title:
                    stats["missing"] += 1
                    continue
                if not dry_run:
                    db.upsert_products([product])
                    row = conn.execute(
                        "SELECT id FROM products WHERE vendor='robu' AND external_id=?;",
                        (product.external_id,),
                    ).fetchone()
                    if row:
                        stats["attributes"] += _persist(
                            conn, int(row[0]), description_html(detail), dry_run=False
                        )
                        stats["described"] += 1
                    db.mark_robu_slug_indexed(slug)
                else:
                    stats["upserted"] += 1

            for item in pending:
                if item["slug"] not in details:
                    stats["missing"] += 1
                    if not dry_run:
                        # Do not retry the same 404 slug forever; a future registry
                        # refresh can reset it to pending.
                        db.mark_robu_slug_status(item["slug"], "missing")
            stats["batches"] += 1
            stats["upserted"] = stats["described"]
            logger.info(
                "batch=%s slugs=%s described=%s attrs=%s pending_left~%s",
                stats["batches"], stats["slugs"], stats["described"],
                stats["attributes"], len(db.get_pending_robu_slugs(limit=1)),
            )
            if sleep_s:
                time.sleep(sleep_s)
    finally:
        client.close()
        conn.close()
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="max slugs (0 = all pending)")
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--sleep", type=float, default=0.2)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    db.init_db()
    stats = run(
        limit=max(0, args.limit),
        batch_size=max(1, args.batch_size),
        sleep_s=max(0.0, args.sleep),
        dry_run=args.dry_run,
    )
    logger.info("Done: %s", stats)
    if not args.dry_run:
        logger.info("Database totals: %s", db.description_stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
