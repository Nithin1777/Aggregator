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
    RobuGraphQLClient,
    RobuGraphQLSchemaError,
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
    timeout: int = 60,
    retries: int = 3,
    backoff: float = 2.0,
) -> Dict[str, int]:
    stats = {"batches": 0, "slugs": 0, "upserted": 0, "described": 0, "attributes": 0, "missing": 0}
    client = RobuGraphQLClient(timeout=timeout, retries=retries, backoff=backoff)
    conn = db.get_db_connection()
    consecutive_failures = 0
    try:
        pending = [
            {"slug": r[0], "url": r[1]}
            for r in conn.execute(
                "SELECT slug, url FROM robu_catalog_registry "
                "WHERE status = 'pending' ORDER BY slug;"
            ).fetchall()
        ]
        if limit:
            pending = pending[:limit]
        if not pending:
            logger.info("No pending Robu slugs left.")
            return stats

        for start in range(0, len(pending), max(1, batch_size)):
            batch = pending[start:start + max(1, batch_size)]
            slugs = [item["slug"] for item in batch]
            try:
                details = client.fetch_batch(slugs, batch_size=batch_size)
            except RobuGraphQLSchemaError as exc:
                logger.error("Stopping: Robu GraphQL schema/query error: %s", exc)
                break
            if client.failures:
                logger.warning(
                    "Skipped %s slug(s) after retries; they remain pending",
                    len(client.failures),
                )
            if not details:
                consecutive_failures += 1
                logger.warning(
                    "No details for this batch (%s consecutive failure(s)); continuing.",
                    consecutive_failures,
                )
                if consecutive_failures >= 5:
                    logger.error(
                        "5 consecutive failed batches; stopping so the endpoint can "
                        "recover. Rerun to retry the remaining pending slugs."
                    )
                    break
                stats["batches"] += 1
                if sleep_s:
                    time.sleep(sleep_s)
                continue
            consecutive_failures = 0

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

            for item in batch:
                if item["slug"] not in details and item["slug"] not in client.failures:
                    stats["missing"] += 1
                    if not dry_run:
                        # A genuine 404: do not retry the same slug forever.
                        db.mark_robu_slug_status(item["slug"], "missing")

            stats["batches"] += 1
            stats["upserted"] = stats["described"]
            pending_left = conn.execute(
                "SELECT COUNT(1) FROM robu_catalog_registry WHERE status = 'pending';"
            ).fetchone()[0]
            logger.info(
                "batch=%s slugs=%s described=%s attrs=%s pending_left=%s",
                stats["batches"], stats["slugs"], stats["described"],
                stats["attributes"], pending_left,
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
    parser.add_argument("--timeout", type=int, default=60, help="per-request timeout (seconds)")
    parser.add_argument("--retries", type=int, default=3, help="retries per request")
    parser.add_argument("--backoff", type=float, default=2.0, help="retry backoff base (seconds)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    db.init_db()
    stats = run(
        limit=max(0, args.limit),
        batch_size=max(1, args.batch_size),
        sleep_s=max(0.0, args.sleep),
        dry_run=args.dry_run,
        timeout=max(5, args.timeout),
        retries=max(1, args.retries),
        backoff=max(0.0, args.backoff),
    )
    logger.info("Done: %s", stats)
    if not args.dry_run:
        logger.info("Database totals: %s", db.description_stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
