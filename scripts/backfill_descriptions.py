"""
Backfill vendor descriptions and free-form attributes.

This is the data pipeline behind `get_product.description` and
`get_product.vendor_specs`. It is intentionally separate from
`scripts/backfill_specs.py` (the curated taxonomy extractor):

  * taxonomy specs  -> product_specs      (filterable)
  * vendor specs    -> product_attributes (fit checks, many more fields)

Vendors:
  * Shopify (/products.json body_html): quartzcomponents, makerbazar, rees52
  * Robokits product pages: robokits
  * Robu product pages: robu (optional; Cloudflare may block the runner's IP)

Usage:
    python scripts/backfill_descriptions.py --vendors quartzcomponents,makerbazar,rees52
    python scripts/backfill_descriptions.py --vendors robokits --limit 100
    python scripts/backfill_descriptions.py --dry-run --limit 20

The script is idempotent: an existing description is skipped unless --force.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db
from adapters.robu_graphql import (
    RobuGraphQLError,
    RobuGraphQLClient,
    description_html,
)
from config import settings
from src.spec.description import description_text, extract_description_attributes

try:
    from curl_cffi import requests as creq
except ImportError:  # pragma: no cover - dependency is in requirements
    creq = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

SHOPIFY_VENDORS = {
    "quartzcomponents": settings.quartz_base_url,
    "makerbazar": settings.makerbazar_base_url,
    "rees52": settings.rees52_base_url,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _session() -> "creq.Session":
    if creq is None:
        raise RuntimeError("curl_cffi is required for the description backfill")
    return creq.Session(impersonate="chrome120", headers=HEADERS)


def _get(conn: sqlite3.Connection, vendor: str, external_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM products WHERE vendor = ? AND external_id = ?;",
        (vendor, str(external_id)),
    ).fetchone()
    return int(row[0]) if row else None


def _already_done(conn: sqlite3.Connection, product_id: int) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM product_descriptions WHERE product_id = ? LIMIT 1;",
            (product_id,),
        ).fetchone()
    )


def _persist(
    conn: sqlite3.Connection,
    product_id: int,
    html: str,
    source: str,
    dry_run: bool,
) -> int:
    attrs = extract_description_attributes(html)
    text = description_text(html)
    if dry_run:
        return len(attrs)
    db.save_product_description(product_id, text, source=source, conn=conn)
    return db.replace_product_attributes(
        product_id,
        attrs,
        source="description",
        conn=conn,
        source_prefix="description",
    )


def _shopify_vendor(
    conn: sqlite3.Connection,
    vendor: str,
    base_url: str,
    page_size: int,
    limit: int,
    max_pages: int,
    sleep_s: float,
    force: bool,
    dry_run: bool,
) -> Dict[str, int]:
    session = _session()
    stats = {"scanned": 0, "matched": 0, "saved": 0, "attributes": 0, "skipped": 0}
    page = 1
    while page <= max_pages:
        try:
            response = session.get(
                f"{base_url.rstrip('/')}/products.json",
                params={"limit": page_size, "page": page},
                timeout=45,
            )
        except Exception as exc:  # noqa: BLE001 - report and stop this vendor
            logger.warning("[%s] page %s failed: %s", vendor, page, exc)
            break
        if response.status_code == 404:
            break
        if response.status_code != 200:
            logger.warning("[%s] page %s HTTP %s", vendor, page, response.status_code)
            break
        items = response.json().get("products", [])
        if not items:
            break

        for item in items:
            stats["scanned"] += 1
            if limit and stats["scanned"] > limit:
                break
            product_id = _get(conn, vendor, str(item.get("id")))
            if product_id is None:
                continue
            stats["matched"] += 1
            if not force and _already_done(conn, product_id):
                stats["skipped"] += 1
                continue
            html = item.get("body_html") or ""
            if not html.strip():
                continue
            saved = _persist(conn, product_id, html, "shopify_body_html", dry_run)
            stats["saved"] += 1
            stats["attributes"] += saved

        logger.info(
            "[%s] page %s/%s: scanned=%s saved=%s attrs=%s",
            vendor, page, max_pages, stats["scanned"], stats["saved"], stats["attributes"],
        )
        if limit and stats["scanned"] >= limit:
            break
        if len(items) < page_size:
            break
        page += 1
        time.sleep(max(0.0, sleep_s))

    session.close()
    return stats


def _robokits_description(soup: Any) -> str:
    from bs4 import BeautifulSoup  # local import keeps startup light

    if isinstance(soup, str):
        soup = BeautifulSoup(soup, "lxml")
    import re

    node = soup.find(id=re.compile(r"productDescription|description", re.I))
    if node is None:
        node = soup.find(attrs={"itemprop": "description"})
    if node is None:
        node = soup.find(["div", "section"], class_=re.compile(r"description", re.I))
    if node is not None:
        return str(node)
    # Robokits embeds the copy inside the product block; this is the safe fallback.
    node = soup.find(id=re.compile(r"productGeneral", re.I))
    return str(node) if node is not None else ""


def _robokits(
    conn: sqlite3.Connection,
    limit: int,
    sleep_s: float,
    force: bool,
    dry_run: bool,
) -> Dict[str, int]:
    from bs4 import BeautifulSoup

    session = _session()
    stats = {"scanned": 0, "matched": 0, "saved": 0, "attributes": 0, "skipped": 0}
    rows = conn.execute(
        "SELECT id, product_url FROM products WHERE vendor = 'robokits' "
        "AND product_url IS NOT NULL AND product_url != '' ORDER BY id;"
    ).fetchall()
    for product_id, url in rows:
        stats["scanned"] += 1
        if limit and stats["scanned"] > limit:
            break
        stats["matched"] += 1
        if not force and _already_done(conn, int(product_id)):
            stats["skipped"] += 1
            continue
        try:
            response = session.get(url, timeout=30)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[robokits] %s failed: %s", url, exc)
            continue
        if response.status_code != 200:
            logger.info("[robokits] skip %s (HTTP %s)", url, response.status_code)
            continue
        soup = BeautifulSoup(response.text, "lxml")
        fragment = _robokits_description(soup)
        if not fragment.strip():
            continue
        saved = _persist(conn, int(product_id), fragment, "robokits_product_page", dry_run)
        stats["saved"] += 1
        stats["attributes"] += saved
        if stats["scanned"] % 25 == 0:
            logger.info(
                "[robokits] scanned=%s saved=%s attrs=%s",
                stats["scanned"], stats["saved"], stats["attributes"],
            )
        time.sleep(max(0.0, sleep_s))
    session.close()
    return stats


def _robu(
    conn: sqlite3.Connection,
    limit: int,
    sleep_s: float,
    force: bool,
    dry_run: bool,
) -> Dict[str, int]:
    """Optional. Robu product pages are Cloudflare-protected; 403s are skipped."""
    from bs4 import BeautifulSoup

    session = _session()
    stats = {"scanned": 0, "matched": 0, "saved": 0, "attributes": 0, "skipped": 0}
    rows = conn.execute(
        "SELECT id, product_url FROM products WHERE vendor = 'robu' "
        "AND product_url IS NOT NULL AND product_url != '' ORDER BY id;"
    ).fetchall()
    for product_id, url in rows:
        stats["scanned"] += 1
        if limit and stats["scanned"] > limit:
            break
        stats["matched"] += 1
        if not force and _already_done(conn, int(product_id)):
            stats["skipped"] += 1
            continue
        try:
            response = session.get(url, timeout=25)
        except Exception as exc:  # noqa: BLE001
            logger.info("[robu] skip %s (%s)", url, exc)
            continue
        if response.status_code != 200:
            logger.info("[robu] skip %s (HTTP %s)", url, response.status_code)
            continue
        soup = BeautifulSoup(response.text, "lxml")
        node = soup.find(attrs={"itemprop": "description"}) or soup.find(
            ["div", "section"], class_=lambda c: c and "description" in " ".join(c).lower()
        )
        if node is None:
            continue
        saved = _persist(conn, int(product_id), str(node), "robu_product_page", dry_run)
        stats["saved"] += 1
        stats["attributes"] += saved
        time.sleep(max(0.0, sleep_s))
    session.close()
    return stats


def _slug_for(row: sqlite3.Row) -> str:
    try:
        raw = json.loads(row["raw_json"] or "{}")
        slug = (raw.get("slug") or "").strip()
    except (TypeError, ValueError):
        slug = ""
    if not slug:
        url = (row["product_url"] or "").rstrip("/")
        slug = url.split("/")[-1] if url else ""
    return slug


def _robu_graphql(
    conn: sqlite3.Connection,
    limit: int,
    batch_size: int,
    sleep_s: float,
    force: bool,
    dry_run: bool,
    update_price_stock: bool = False,
) -> Dict[str, int]:
    """
    Robu product descriptions via the frontend's ProductDetails GraphQL API.

    This is the preferred Robu path: JSON, no Cloudflare HTML, and aliases allow
    `batch_size` products per request.
    """
    stats = {"scanned": 0, "matched": 0, "saved": 0, "attributes": 0, "skipped": 0}
    if force:
        rows = conn.execute(
            "SELECT id, raw_json, product_url FROM products WHERE vendor = 'robu' ORDER BY id;"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT p.id, p.raw_json, p.product_url FROM products p "
            "LEFT JOIN product_descriptions d ON d.product_id = p.id "
            "WHERE p.vendor = 'robu' AND d.product_id IS NULL ORDER BY p.id;"
        ).fetchall()

    if limit:
        rows = rows[:limit]
    stats["scanned"] = len(rows)
    stats["matched"] = len(rows)
    if not rows:
        return stats

    client = RobuGraphQLClient()
    try:
        for start in range(0, len(rows), max(1, batch_size)):
            chunk = rows[start:start + max(1, batch_size)]
            by_slug = {_slug_for(row): row for row in chunk if _slug_for(row)}
            if not by_slug:
                continue
            try:
                details = client.fetch_batch(list(by_slug.keys()), batch_size=batch_size)
            except RobuGraphQLError as exc:
                logger.warning("[robu-graphql] stopped at offset %s: %s", start, exc)
                break
            for slug, detail in details.items():
                row = by_slug.get(slug)
                if row is None:
                    continue
                html = description_html(detail)
                if not html.strip():
                    stats["skipped"] += 1
                    continue
                saved = _persist(
                    conn, int(row["id"]), html, "robu_graphql", dry_run
                )
                stats["saved"] += 1
                stats["attributes"] += saved
                if update_price_stock and not dry_run:
                    price = detail.get("price") or {}
                    final_price = price.get("final") or price.get("regular")
                    try:
                        final_price = float(final_price)
                    except (TypeError, ValueError):
                        final_price = None
                    if final_price and final_price > 0:
                        db.update_price_stock_batch([{
                            "id": int(row["id"]),
                            "price": final_price,
                            "available": bool(detail.get("in_stock")),
                            "fetched_at": db.utc_now_iso(),
                        }])
            if stats["saved"] and stats["saved"] % 200 < batch_size:
                logger.info(
                    "[robu-graphql] saved=%s attrs=%s of %s",
                    stats["saved"], stats["attributes"], len(rows),
                )
            time.sleep(max(0.0, sleep_s))
    finally:
        client.close()
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vendors",
        default="quartzcomponents,makerbazar,rees52",
        help="comma-separated vendor keys",
    )
    parser.add_argument("--limit", type=int, default=0, help="max products per vendor (0 = all)")
    parser.add_argument("--max-pages", type=int, default=200, help="max Shopify catalog pages")
    parser.add_argument("--page-size", type=int, default=250, help="Shopify page size")
    parser.add_argument("--sleep", type=float, default=0.35, help="delay between requests")
    parser.add_argument(
        "--batch-size", type=int, default=10, help="Robu GraphQL products per request"
    )
    parser.add_argument(
        "--robu-source",
        choices=["graphql", "html"],
        default="graphql",
        help="Robu transport: GraphQL API (default) or product pages",
    )
    parser.add_argument(
        "--update-price-stock",
        action="store_true",
        help="also write price/stock returned by the Robu GraphQL API",
    )
    parser.add_argument("--force", action="store_true", help="refetch even if a description exists")
    parser.add_argument("--dry-run", action="store_true", help="parse but do not write")
    args = parser.parse_args(argv)

    vendors = [v.strip() for v in args.vendors.split(",") if v.strip()]
    conn = db.get_db_connection()
    try:
        db.init_db()
        totals: Dict[str, Dict[str, int]] = {}
        for vendor in vendors:
            if vendor in SHOPIFY_VENDORS:
                totals[vendor] = _shopify_vendor(
                    conn,
                    vendor,
                    SHOPIFY_VENDORS[vendor],
                    page_size=max(1, min(args.page_size, 250)),
                    limit=max(0, args.limit),
                    max_pages=max(1, args.max_pages),
                    sleep_s=max(0.0, args.sleep),
                    force=args.force,
                    dry_run=args.dry_run,
                )
            elif vendor == "robokits":
                totals[vendor] = _robokits(
                    conn, max(0, args.limit), max(0.0, args.sleep), args.force, args.dry_run
                )
            elif vendor == "robu":
                if args.robu_source == "graphql":
                    totals[vendor] = _robu_graphql(
                        conn,
                        max(0, args.limit),
                        max(1, args.batch_size),
                        max(0.0, args.sleep),
                        args.force,
                        args.dry_run,
                        update_price_stock=args.update_price_stock,
                    )
                else:
                    totals[vendor] = _robu(
                        conn, max(0, args.limit), max(0.0, args.sleep), args.force, args.dry_run
                    )
            else:
                logger.warning("Unknown vendor %r; skipping.", vendor)

        logger.info("Backfill complete (dry_run=%s)", args.dry_run)
        for vendor, stats in totals.items():
            logger.info(
                "  %-18s scanned=%-6s saved=%-6s attrs=%-7s skipped=%s",
                vendor, stats["scanned"], stats["saved"], stats["attributes"], stats["skipped"],
            )
        if not args.dry_run:
            logger.info("Database totals: %s", db.description_stats())
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
