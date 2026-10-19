"""
SQLite storage layer with Full-Text Search (FTS5) and continuable scraping checkpointing.
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from config import settings
from models import ScrapeState, UnifiedProduct

logger = logging.getLogger(__name__)

def get_db_connection() -> sqlite3.Connection:
    """Creates a configured SQLite connection."""
    conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_db():
    """Initializes schema, indexes, FTS5 virtual table, and checkpoint tracking."""
    conn = get_db_connection()
    try:
        with conn:
            # 1. Main Products Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vendor TEXT NOT NULL,
                    vendor_name TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    normalized_title TEXT NOT NULL,
                    mpn TEXT,
                    sku TEXT,
                    price REAL NOT NULL DEFAULT 0.0,
                    available INTEGER NOT NULL DEFAULT 0,
                    product_url TEXT NOT NULL,
                    image_url TEXT,
                    category TEXT,
                    is_canonical INTEGER NOT NULL DEFAULT 0,
                    canonical_for TEXT,
                    query_ctr REAL NULL,
                    raw_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(vendor, external_id)
                );
            """)

            # 2. Indexes for fast filtering
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_vendor ON products(vendor);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_mpn ON products(mpn);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_price ON products(price);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_available ON products(available);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_products_canonical ON products(is_canonical, canonical_for);")

            # 2b. Variant families table for Option B
            conn.execute("""
                CREATE TABLE IF NOT EXISTS variant_families (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_mpn TEXT NOT NULL,
                    family_label TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_variant_families_mpn ON variant_families(canonical_mpn);")


            # 3. FTS5 Virtual Table for Instant Local Full-Text Search
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS products_fts USING fts5(
                    title,
                    normalized_title,
                    mpn,
                    sku,
                    vendor,
                    content='products',
                    content_rowid='id'
                );
            """)

            # 4. Triggers to keep FTS5 synchronized with main products table
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_products_ai AFTER INSERT ON products BEGIN
                    INSERT INTO products_fts(rowid, title, normalized_title, mpn, sku, vendor)
                    VALUES (new.id, new.title, new.normalized_title, new.mpn, new.sku, new.vendor);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_products_ad AFTER DELETE ON products BEGIN
                    INSERT INTO products_fts(products_fts, rowid, title, normalized_title, mpn, sku, vendor)
                    VALUES('delete', old.id, old.title, old.normalized_title, old.mpn, old.sku, old.vendor);
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_products_au AFTER UPDATE ON products BEGIN
                    INSERT INTO products_fts(products_fts, rowid, title, normalized_title, mpn, sku, vendor)
                    VALUES('delete', old.id, old.title, old.normalized_title, old.mpn, old.sku, old.vendor);
                    INSERT INTO products_fts(rowid, title, normalized_title, mpn, sku, vendor)
                    VALUES (new.id, new.title, new.normalized_title, new.mpn, new.sku, new.vendor);
                END;
            """)

            # 5. Scraping Checkpoint Table (Enables continuable / resumable batch ingestion)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scrape_checkpoints (
                    vendor TEXT PRIMARY KEY,
                    last_page INTEGER NOT NULL DEFAULT 0,
                    last_offset INTEGER NOT NULL DEFAULT 0,
                    total_ingested INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 6. Robu Ground-Truth Catalog Registry (Populated from sitemaps to guarantee 100% catalog coverage)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS robu_catalog_registry (
                    slug TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_robu_registry_status ON robu_catalog_registry(status);")

            # 7. Gray-Zone Telemetry & Audit Log Table (Tracks ambiguous merges and vetoes)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS matcher_gray_zone (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    query TEXT,
                    vendor_a TEXT NOT NULL,
                    title_a TEXT NOT NULL,
                    mpn_a TEXT,
                    price_a REAL DEFAULT 0.0,
                    vendor_b TEXT NOT NULL,
                    title_b TEXT NOT NULL,
                    mpn_b TEXT,
                    price_b REAL DEFAULT 0.0,
                    score REAL DEFAULT 0.0,
                    decision TEXT NOT NULL,
                    risk_level TEXT NOT NULL DEFAULT 'medium',
                    flags TEXT,
                    reason TEXT NOT NULL
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gray_zone_risk ON matcher_gray_zone(risk_level);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gray_zone_time ON matcher_gray_zone(timestamp);")

            # 8. UI Click Telemetry Table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ui_clicks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    query TEXT,
                    group_id TEXT,
                    vendor TEXT,
                    product_id TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ui_clicks_ts ON ui_clicks(ts);")

            # 9. Taxonomy-driven spec tables + products.spec_category.
            #    Applied here (idempotently) so a clean checkout gets the full schema:
            #    without it, product_specs / spec_stats_global are missing and
            #    products.spec_category does not exist, which breaks the MCP server
            #    and every spec query on a fresh database.
            try:
                from src.spec.migration import run_spec_migration
                run_spec_migration(conn)
            except Exception as exc:  # noqa: BLE001 - base init must not fail on this
                logger.warning("Spec migration could not be applied: %s", exc)
    finally:
        conn.close()


def record_click(query: str, group_id: str, vendor: str, product_id: str) -> int:
    """Records a user click event in ui_clicks table."""
    conn = get_db_connection()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO ui_clicks (query, group_id, vendor, product_id)
                VALUES (?, ?, ?, ?)
                """,
                (query, group_id, vendor, product_id)
            )
            return cursor.lastrowid
    finally:
        conn.close()


def get_click_count() -> int:
    """Returns total recorded clicks in ui_clicks."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM ui_clicks").fetchone()
        return row["cnt"] if row else 0
    finally:
        conn.close()


def upsert_products(products: List[UnifiedProduct]) -> int:
    """
    Inserts or updates a batch of UnifiedProducts into the database.
    Returns the count of successfully processed products.
    """
    if not products:
        return 0

    conn = get_db_connection()
    count = 0
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            for p in products:
                raw = json.dumps(p.extra) if p.extra else "{}"
                conn.execute("""
                    INSERT INTO products (
                        vendor, vendor_name, external_id, title, normalized_title,
                        mpn, sku, price, available, product_url, image_url,
                        category, raw_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(vendor, external_id) DO UPDATE SET
                        vendor_name = excluded.vendor_name,
                        title = excluded.title,
                        normalized_title = excluded.normalized_title,
                        mpn = excluded.mpn,
                        sku = excluded.sku,
                        price = excluded.price,
                        available = excluded.available,
                        product_url = excluded.product_url,
                        image_url = excluded.image_url,
                        category = excluded.category,
                        raw_json = excluded.raw_json,
                        updated_at = excluded.updated_at;
                """, (
                    p.vendor,
                    p.vendor_name,
                    p.external_id,
                    p.title,
                    p.normalized_title,
                    p.mpn,
                    p.sku,
                    p.price,
                    1 if p.available else 0,
                    p.product_url,
                    p.image_url,
                    p.category,
                    raw,
                    now_str
                ))
                if p.vendor == "robu" and p.extra and p.extra.get("slug"):
                    conn.execute("""
                        INSERT INTO robu_catalog_registry (slug, url, status, updated_at)
                        VALUES (?, ?, 'indexed', ?)
                        ON CONFLICT(slug) DO UPDATE SET status = 'indexed', updated_at = excluded.updated_at;
                    """, (p.extra["slug"], p.product_url, now_str))
                count += 1
    finally:
        conn.close()
    return count

def populate_robu_registry(slug_url_tuples: List[tuple]) -> int:
    """Populates Robu ground-truth registry from sitemap URLs."""
    if not slug_url_tuples:
        return 0
    conn = get_db_connection()
    inserted = 0
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            for slug, url in slug_url_tuples:
                res = conn.execute("""
                    INSERT OR IGNORE INTO robu_catalog_registry (slug, url, status, created_at, updated_at)
                    VALUES (?, ?, 'pending', ?, ?);
                """, (slug, url, now_str, now_str))
                inserted += res.rowcount
    finally:
        conn.close()
    return inserted

def get_robu_registry_stats() -> Dict[str, Any]:
    """Returns total, indexed, and pending count of Robu products in the registry."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as total FROM robu_catalog_registry;")
        total = cursor.fetchone()["total"]
        cursor.execute("SELECT COUNT(*) as indexed FROM robu_catalog_registry WHERE status = 'indexed';")
        indexed = cursor.fetchone()["indexed"]
        return {
            "total_registry": total,
            "indexed": indexed,
            "pending": total - indexed,
            "coverage_pct": round((indexed / total * 100), 1) if total > 0 else 0.0
        }
    finally:
        conn.close()

def get_pending_robu_slugs(limit: int = 100) -> List[Dict[str, str]]:
    """Returns next slice of pending Robu slugs that have not yet been indexed."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT slug, url FROM robu_catalog_registry WHERE status = 'pending' LIMIT ?;", (limit,))
        return [{"slug": row["slug"], "url": row["url"]} for row in cursor.fetchall()]
    finally:
        conn.close()

def mark_robu_slug_indexed(slug: str) -> None:
    """Marks a slug as indexed or checked in the registry."""
    conn = get_db_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            conn.execute("""
                UPDATE robu_catalog_registry
                SET status = 'indexed', updated_at = ?
                WHERE slug = ?;
            """, (now_str, slug))
    finally:
        conn.close()

def sync_robu_registry_with_products() -> int:
    """Updates registry status to 'indexed' for all products currently in the database."""
    conn = get_db_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            cursor = conn.execute("""
                UPDATE robu_catalog_registry
                SET status = 'indexed', updated_at = ?
                WHERE status != 'indexed' AND slug IN (
                    SELECT json_extract(raw_json, '$.slug')
                    FROM products
                    WHERE vendor = 'robu' AND json_extract(raw_json, '$.slug') IS NOT NULL
                );
            """, (now_str,))
            return cursor.rowcount
    finally:
        conn.close()

def get_checkpoint(vendor: str) -> ScrapeState:
    """Retrieves current scraping checkpoint for a vendor."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scrape_checkpoints WHERE vendor = ?", (vendor,))
        row = cursor.fetchone()
        if row:
            return ScrapeState(
                vendor=row["vendor"],
                last_page=row["last_page"],
                last_offset=row["last_offset"],
                total_ingested=row["total_ingested"],
                status=row["status"],
                last_error=row["last_error"],
                updated_at=str(row["updated_at"]) if row["updated_at"] else None
            )
        return ScrapeState(vendor=vendor, last_page=0, last_offset=0, total_ingested=0, status="idle")
    finally:
        conn.close()

def save_checkpoint(
    vendor: str,
    last_page: int,
    last_offset: int,
    total_ingested: int,
    status: str = "in_progress",
    last_error: Optional[str] = None
):
    """Updates or sets the scraping checkpoint for a vendor."""
    conn = get_db_connection()
    now_str = datetime.now(timezone.utc).isoformat()
    try:
        with conn:
            conn.execute("""
                INSERT INTO scrape_checkpoints (vendor, last_page, last_offset, total_ingested, status, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vendor) DO UPDATE SET
                    last_page = excluded.last_page,
                    last_offset = excluded.last_offset,
                    total_ingested = excluded.total_ingested,
                    status = excluded.status,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at;
            """, (vendor, last_page, last_offset, total_ingested, status, last_error, now_str))
    finally:
        conn.close()

def reset_checkpoint(vendor: str):
    """Resets the scraping checkpoint to start from page 1 / offset 0."""
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM scrape_checkpoints WHERE vendor = ?", (vendor,))
    finally:
        conn.close()

def search_products(
    query: str,
    limit: int = 50,
    offset: int = 0,
    vendor: Optional[str] = None,
    in_stock_only: bool = False
) -> List[UnifiedProduct]:
    """
    Performs search using FTS5 with fallback to LIKE pattern matching.
    """
    conn = get_db_connection()
    results: List[UnifiedProduct] = []
    clean_q = query.strip()
    if not clean_q:
        return []

    try:
        cursor = conn.cursor()
        # Attempt FTS5 query with compounding expansion
        sanitized = re.sub(r'["\*\(\)\:]', '', clean_q).strip()
        tokens = sanitized.split()
        variations = []
        if tokens:
            primary = " ".join([f'"{t}"*' for t in tokens])
            variations.append(f"({primary})")
            if len(tokens) > 1:
                full_concat = "".join(tokens)
                if len(full_concat) >= 3:
                    variations.append(f'("{full_concat}"*)')
                if len(tokens) > 2:
                    for i in range(len(tokens) - 1):
                        merged_bigram = tokens[i] + tokens[i+1]
                        sub_tokens = tokens[:i] + [merged_bigram] + tokens[i+2:]
                        var_q = " ".join([f'"{t}"*' for t in sub_tokens])
                        variations.append(f"({var_q})")
            else:
                m_split = re.match(r'^([a-zA-Z]{2,})(\d+)$', tokens[0])
                if m_split:
                    variations.append(f'("{m_split.group(1)}"* "{m_split.group(2)}"*)')

        seen_vars = set()
        deduped_vars = [v for v in variations if not (v in seen_vars or seen_vars.add(v))]
        fts_query = " OR ".join(deduped_vars) if deduped_vars else f'"{clean_q}"*'

        where_clauses = ["products_fts MATCH ?"]
        params: List[Any] = [fts_query]


        if vendor:
            where_clauses.append("p.vendor = ?")
            params.append(vendor)
        if in_stock_only:
            where_clauses.append("p.available = 1")

        sql = f"""
            SELECT p.* FROM products p
            JOIN products_fts fts ON p.id = fts.rowid
            WHERE {" AND ".join(where_clauses)}
            ORDER BY rank
            LIMIT ? OFFSET ?;
        """
        params.extend([limit, offset])

        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Fallback to standard LIKE if FTS fails
            like_clauses = ["(p.title LIKE ? OR p.normalized_title LIKE ? OR p.mpn LIKE ? OR p.sku LIKE ?)"]
            like_pat = f"%{clean_q}%"
            params = [like_pat, like_pat, like_pat, like_pat]
            if vendor:
                like_clauses.append("p.vendor = ?")
                params.append(vendor)
            if in_stock_only:
                like_clauses.append("p.available = 1")

            sql_like = f"""
                SELECT p.* FROM products p
                WHERE {" AND ".join(like_clauses)}
                ORDER BY p.available DESC, p.price ASC
                LIMIT ? OFFSET ?;
            """
            params.extend([limit, offset])
            cursor.execute(sql_like, params)
            rows = cursor.fetchall()

        for r in rows:
            results.append(UnifiedProduct(
                id=r["id"],
                vendor=r["vendor"],
                vendor_name=r["vendor_name"],
                external_id=r["external_id"],
                title=r["title"],
                normalized_title=r["normalized_title"],
                mpn=r["mpn"],
                sku=r["sku"],
                price=float(r["price"]),
                available=bool(r["available"]),
                product_url=r["product_url"],
                image_url=r["image_url"],
                category=r["category"],
                is_canonical=bool(r["is_canonical"]) if "is_canonical" in r.keys() and r["is_canonical"] else False,
                canonical_for=r["canonical_for"] if "canonical_for" in r.keys() else None,
                query_ctr=float(r["query_ctr"]) if "query_ctr" in r.keys() and r["query_ctr"] is not None else None,
                updated_at=str(r["updated_at"]) if r["updated_at"] else None,
                extra=json.loads(r["raw_json"]) if r["raw_json"] else {}
            ))

    finally:
        conn.close()

    return results

def get_db_stats() -> Dict[str, Any]:
    """Returns database summary statistics and vendor checkpoints."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total FROM products;")
        total_products = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS in_stock FROM products WHERE available = 1;")
        in_stock_products = cursor.fetchone()["in_stock"]

        cursor.execute("""
            SELECT vendor, COUNT(*) AS count, SUM(available) AS in_stock
            FROM products GROUP BY vendor;
        """)
        vendors = {
            row["vendor"]: {
                "total": row["count"],
                "in_stock": row["in_stock"] or 0
            } for row in cursor.fetchall()
        }

        cursor.execute("SELECT * FROM scrape_checkpoints;")
        checkpoints = {
            row["vendor"]: {
                "last_page": row["last_page"],
                "last_offset": row["last_offset"],
                "total_ingested": row["total_ingested"],
                "status": row["status"],
                "last_error": row["last_error"],
                "updated_at": str(row["updated_at"]) if row["updated_at"] else None
            } for row in cursor.fetchall()
        }

        return {
            "total_products": total_products,
            "in_stock_products": in_stock_products,
            "vendors": vendors,
            "checkpoints": checkpoints,
            "robu_registry": get_robu_registry_stats()
        }
    finally:
        conn.close()
