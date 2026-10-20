"""
SQLite storage layer with Full-Text Search (FTS5) and continuable scraping checkpointing.
"""
import difflib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from config import settings
from src.search.query_terms import (
    QueryPlan,
    content_tokens,
    correctable,
    detect_unsupported_syntax,
    fts_variations,
    relaxed_fts_expr,
    strict_fts_expr,
)
from models import ScrapeState, UnifiedProduct
from src.spec.filters import (
    build_spec_clauses,
    load_catalog_specs,
    parse_and_validate_filters,
)
from src.spec.taxonomy_loader import (
    classify_product,
    get_taxonomy,
    is_junk_raw,
    tokenize,
)

logger = logging.getLogger(__name__)

# SQLite caps host parameters at 999 per statement; stay well below it for IN clauses.
_SQL_CHUNK = 500

def get_db_connection() -> sqlite3.Connection:
    """Creates a configured SQLite connection."""
    conn = sqlite3.connect(str(settings.db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    # Keep SQLite's temporary b-trees (index builds, GROUP BY sorts) in RAM.
    # The default is to spill them to the OS temp directory, which is not
    # always writable (sandboxes, locked-down CI) and makes SQLite fail with
    # "unable to open database file" on perfectly valid statements.
    conn.execute("PRAGMA temp_store = MEMORY;")
    return conn


def utc_now_iso() -> str:
    """Single timestamp format used for all refresh bookkeeping.

    UTC ISO-8601 strings written in this exact format sort lexicographically,
    which lets stale-cache queries compare them directly in SQL.
    """
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Short-lived in-process caches
#
# Aggregate aggregates the whole catalog on every page render (category and
# vendor counts, catalog stats). Those numbers only change when the catalog is
# re-ingested, so they are cached for a few seconds instead of re-scanning
# 100k+ rows on every request.
# ---------------------------------------------------------------------------
_CACHE_TTL_SECONDS = 30
_categories_cache: Dict[str, Any] = {"at": 0.0, "value": None}
_vendor_counts_cache: Dict[str, Any] = {"at": 0.0, "value": None}
_stats_cache: Dict[str, Any] = {"at": 0.0, "value": None}


def _cache_get(cache: Dict[str, Any]) -> Any:
    """Returns a cached value when it is still fresh, otherwise None."""
    if cache["value"] is None:
        return None
    if (time.monotonic() - cache["at"]) >= _CACHE_TTL_SECONDS:
        return None
    return cache["value"]


def _cache_set(cache: Dict[str, Any], value: Any) -> None:
    """Stores a value in the cache with a fresh timestamp."""
    cache["at"] = time.monotonic()
    cache["value"] = value


def invalidate_counts_cache() -> None:
    """Drops the cached catalog aggregates (after an ingest or a refresh)."""
    for cache in (_categories_cache, _vendor_counts_cache, _stats_cache):
        cache["at"] = 0.0
        cache["value"] = None


def _ensure_price_stock_schema(conn: sqlite3.Connection) -> None:
    """
    Idempotently adds the price/stock refresh cache columns to `products`.

    - price_stock_updated_at  : last successful live price/stock fetch
    - price_stock_attempted_at: last refresh attempt (success or failure); drives the TTL
    - price_stock_source      : 'live' (fetched from vendor) or 'cache' (never refreshed)
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(products);")
    existing = {row[1] for row in cur.fetchall()}
    if "price_stock_updated_at" not in existing:
        conn.execute("ALTER TABLE products ADD COLUMN price_stock_updated_at TIMESTAMP;")
    if "price_stock_attempted_at" not in existing:
        conn.execute("ALTER TABLE products ADD COLUMN price_stock_attempted_at TIMESTAMP;")
    if "price_stock_source" not in existing:
        conn.execute("ALTER TABLE products ADD COLUMN price_stock_source TEXT DEFAULT 'cache';")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_price_stock_attempted "
        "ON products(price_stock_attempted_at);"
    )


def _ensure_description_schema(conn: sqlite3.Connection) -> None:
    """
    Vendor descriptions and the free-form attributes extracted from them.

    `product_specs` stays the taxonomy-filterable layer. Vendor tables often
    carry dozens of attributes the taxonomy does not define yet (tolerance,
    package, dielectric, interface, ...); they live here so agents can answer
    fit questions without waiting for a taxonomy migration.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_descriptions (
            product_id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'vendor_page',
            fetched_at TEXT
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_attributes (
            product_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            label TEXT,
            value TEXT NOT NULL,
            value_num REAL,
            unit TEXT,
            source TEXT NOT NULL DEFAULT 'description_table',
            confidence REAL DEFAULT 1.0,
            PRIMARY KEY (product_id, key)
        );
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_product_attributes_key "
        "ON product_attributes(key);"
    )


def _ensure_category_schema(conn: sqlite3.Connection) -> None:
    """
    Idempotently adds the canonical category layer.

    * products.category_canonical - slug from `config/taxonomy.yaml`
      (the raw `products.category` column holds 3,181 vendor specific strings
      and cannot be filtered on directly).
    * category_map                - raw vendor category -> canonical slug,
      kept so the mapping stays auditable and can be re-tuned without a full
      re-scan.
    * category_taxonomy           - cached taxonomy metadata for the UI.
    * search_queries              - search log powering popular-search hints.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(products);")
    existing = {row[1] for row in cur.fetchall()}
    if "category_canonical" not in existing:
        conn.execute("ALTER TABLE products ADD COLUMN category_canonical TEXT;")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_category_canonical "
        "ON products(category_canonical);"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_category_vendor "
        "ON products(category_canonical, vendor);"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS category_map (
            raw_category TEXT PRIMARY KEY,
            category_slug TEXT NOT NULL,
            matched_keyword TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_category_map_slug ON category_map(category_slug);"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS category_taxonomy (
            slug TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT 'package',
            description TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_queries (
            query TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_queries_count ON search_queries(count DESC);"
    )


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

            # 2a. Price/Stock refresh cache columns (search-time refresh + cached fallback)
            _ensure_price_stock_schema(conn)

            # 2b. Canonical category layer (user facing taxonomy + facets)
            _ensure_category_schema(conn)

            # 2c. Vendor descriptions and free-form attributes
            _ensure_description_schema(conn)

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
                        category, raw_json, updated_at,
                        price_stock_updated_at, price_stock_attempted_at, price_stock_source
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        updated_at = excluded.updated_at,
                        price_stock_updated_at = excluded.price_stock_updated_at,
                        price_stock_attempted_at = excluded.price_stock_attempted_at,
                        price_stock_source = excluded.price_stock_source;
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
                    now_str,
                    now_str,
                    now_str,
                    "live"
                ))
                if p.vendor == "robu" and p.extra and p.extra.get("slug"):
                    conn.execute("""
                        INSERT INTO robu_catalog_registry (slug, url, status, updated_at)
                        VALUES (?, ?, 'indexed', ?)
                        ON CONFLICT(slug) DO UPDATE SET status = 'indexed', updated_at = excluded.updated_at;
                    """, (p.extra["slug"], p.product_url, now_str))
                count += 1
        invalidate_counts_cache()
    finally:
        conn.close()
    return count

def ensure_description_schema(conn: Optional[sqlite3.Connection] = None) -> None:
    """Creates product_descriptions/product_attributes if an older DB lacks them."""
    own = conn is None
    conn = conn or get_db_connection()
    try:
        with conn:
            _ensure_description_schema(conn)
    finally:
        if own:
            conn.close()


def save_product_description(
    product_id: int,
    description: str,
    source: str = "vendor_page",
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Stores the cleaned vendor description (idempotent per product)."""
    own = conn is None
    conn = conn or get_db_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO product_descriptions (product_id, description, source, fetched_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(product_id) DO UPDATE SET
                    description = excluded.description,
                    source = excluded.source,
                    fetched_at = excluded.fetched_at;
                """,
                (int(product_id), description or "", source, utc_now_iso()),
            )
    finally:
        if own:
            conn.close()


def get_product_description(product_id: int) -> Optional[Dict[str, Any]]:
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT description, source, fetched_at FROM product_descriptions WHERE product_id = ?;",
            (int(product_id),),
        ).fetchone()
        if not row:
            return None
        return {
            "description": row[0],
            "source": row[1],
            "fetched_at": row[2],
        }
    finally:
        conn.close()


def replace_product_attributes(
    product_id: int,
    attributes: List[Dict[str, Any]],
    source: str = "description",
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """
    Replaces the free-form attributes for one product. Idempotent.

    `attributes` items use the `src.spec.description` shape:
    key, label, value, value_num, unit, confidence.
    """
    own = conn is None
    conn = conn or get_db_connection()
    try:
        with conn:
            conn.execute(
                "DELETE FROM product_attributes WHERE product_id = ?;",
                (int(product_id),),
            )
            rows = [
                (
                    int(product_id),
                    str(item["key"])[:80],
                    str(item.get("label") or item["key"])[:120],
                    str(item["value"])[:500],
                    item.get("value_num"),
                    item.get("unit"),
                    item.get("source") or source,
                    float(item.get("confidence", 1.0)),
                )
                for item in attributes
                if item.get("key") and item.get("value") is not None
            ]
            if rows:
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO product_attributes
                        (product_id, key, label, value, value_num, unit, source, confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    rows,
                )
            return len(rows)
    finally:
        if own:
            conn.close()


def get_product_attributes(product_id: int) -> Dict[str, Dict[str, Any]]:
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT key, label, value, value_num, unit, source, confidence "
            "FROM product_attributes WHERE product_id = ? ORDER BY key;",
            (int(product_id),),
        ).fetchall()
        return {
            row[0]: {
                "label": row[1],
                "value": row[2],
                "value_num": row[3],
                "unit": row[4],
                "source": row[5],
                "confidence": row[6],
            }
            for row in rows
        }
    finally:
        conn.close()


def get_attribute_counts(product_ids: List[int]) -> Dict[int, int]:
    """Number of vendor attributes per product, for compact list payloads."""
    if not product_ids:
        return {}
    placeholders = ", ".join(["?"] * len(product_ids))
    conn = get_db_connection()
    try:
        rows = conn.execute(
            f"SELECT product_id, COUNT(1) FROM product_attributes "
            f"WHERE product_id IN ({placeholders}) GROUP BY product_id;",
            list(product_ids),
        ).fetchall()
        return {int(r[0]): int(r[1]) for r in rows}
    finally:
        conn.close()


def description_stats() -> Dict[str, int]:
    conn = get_db_connection()
    try:
        return {
            "descriptions": int(
                conn.execute("SELECT COUNT(1) FROM product_descriptions;").fetchone()[0]
            ),
            "attribute_rows": int(
                conn.execute("SELECT COUNT(1) FROM product_attributes;").fetchone()[0]
            ),
            "products_with_attributes": int(
                conn.execute(
                    "SELECT COUNT(DISTINCT product_id) FROM product_attributes;"
                ).fetchone()[0]
            ),
        }
    finally:
        conn.close()


def row_to_unified_product(row: sqlite3.Row) -> UnifiedProduct:
    """Maps a `products` row (including optional refresh columns) to UnifiedProduct."""
    keys = row.keys()

    def _get(name: str, default: Any = None) -> Any:
        return row[name] if name in keys else default

    price_updated_at = _get("price_stock_updated_at")
    price_source = _get("price_stock_source")
    extra = json.loads(row["raw_json"]) if _get("raw_json") else {}
    price = float(row["price"])
    # Some vendors quote ex-GST prices while the catalog stores tax-inclusive
    # values, so derive the note from the stored price (a refresh only rewrites
    # `price`, which would leave an ingest-time note stale).
    price_note = extra.get("price_note") if isinstance(extra, dict) else None
    gst_rate = 0.0
    if row["vendor"] == "robokits":
        gst_rate = max(0.0, float(getattr(settings, "robokits_gst_rate", 0.18) or 0.0))
    elif row["vendor"] == "quartzcomponents":
        gst_rate = max(0.0, float(getattr(settings, "quartz_gst_rate", 0.18) or 0.0))
    if gst_rate > 0 and price > 0:
        base = round(price / (1.0 + gst_rate), 2)
        price_note = (
            f"₹{base:,.2f} + {gst_rate * 100:.0f}% GST (tax-inclusive price shown)"
        )
    return UnifiedProduct(
        id=row["id"],
        vendor=row["vendor"],
        vendor_name=row["vendor_name"],
        external_id=row["external_id"],
        title=row["title"],
        normalized_title=row["normalized_title"],
        mpn=row["mpn"],
        sku=row["sku"],
        price=price,
        available=bool(row["available"]),
        product_url=row["product_url"],
        image_url=row["image_url"],
        category=row["category"],
        category_canonical=_get("category_canonical"),
        is_canonical=bool(_get("is_canonical")),
        canonical_for=_get("canonical_for"),
        query_ctr=float(_get("query_ctr")) if _get("query_ctr") is not None else None,
        updated_at=str(row["updated_at"]) if row["updated_at"] else None,
        price_updated_at=str(price_updated_at) if price_updated_at else None,
        price_source=price_source if price_source else None,
        price_note=price_note,
        extra=extra,
    )


def get_products_by_ids(product_ids: List[int]) -> List[UnifiedProduct]:
    """Loads full product models for the given IDs (chunked for SQLite parameter limits)."""
    ids = [int(i) for i in product_ids if i is not None]
    if not ids:
        return []

    conn = get_db_connection()
    results: List[UnifiedProduct] = []
    try:
        for start in range(0, len(ids), _SQL_CHUNK):
            chunk = ids[start:start + _SQL_CHUNK]
            placeholders = ", ".join(["?"] * len(chunk))
            rows = conn.execute(
                f"SELECT * FROM products WHERE id IN ({placeholders});", chunk
            ).fetchall()
            results.extend(row_to_unified_product(r) for r in rows)
    finally:
        conn.close()
    return results


def get_stale_price_stock_ids(
    product_ids: List[int],
    ttl_seconds: int,
    force: bool = False,
) -> List[int]:
    """
    Returns the subset of product IDs whose price/stock cache is stale.

    A product is stale when it has never been attempted, or when the last attempt
    is older than `ttl_seconds` (or `force=True`). Failed attempts also refresh
    this marker, so an offline catalog is not hammered on every page view.
    """
    ids = [int(i) for i in product_ids if i is not None]
    if not ids:
        return []

    conn = get_db_connection()
    stale: List[int] = []
    try:
        for start in range(0, len(ids), _SQL_CHUNK):
            chunk = ids[start:start + _SQL_CHUNK]
            placeholders = ", ".join(["?"] * len(chunk))
            if force:
                rows = conn.execute(
                    f"SELECT id FROM products WHERE id IN ({placeholders});", chunk
                ).fetchall()
            else:
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(seconds=max(0, int(ttl_seconds)))
                ).isoformat()
                rows = conn.execute(
                    f"""
                    SELECT id FROM products
                    WHERE id IN ({placeholders})
                      AND (price_stock_attempted_at IS NULL OR price_stock_attempted_at <= ?);
                    """,
                    chunk + [cutoff],
                ).fetchall()
            stale.extend(int(r["id"]) for r in rows)
    finally:
        conn.close()
    return stale


def update_price_stock_batch(updates: List[Dict[str, Any]]) -> int:
    """
    Persists refreshed price/availability for existing products.

    Deliberately touches ONLY price, stock and the refresh-cache metadata: titles,
    specs, descriptions, URLs and any other catalog fields stay exactly as ingested.

    Each update dict: {id, price, available, fetched_at}.
    Returns the number of rows updated.
    """
    if not updates:
        return 0

    conn = get_db_connection()
    updated = 0
    try:
        with conn:
            for u in updates:
                if u.get("id") is None:
                    continue
                fetched_at = u.get("fetched_at") or utc_now_iso()
                cur = conn.execute(
                    """
                    UPDATE products
                    SET price = ?,
                        available = ?,
                        price_stock_updated_at = ?,
                        price_stock_attempted_at = ?,
                        price_stock_source = 'live'
                    WHERE id = ?;
                    """,
                    (
                        float(u.get("price") or 0.0),
                        1 if u.get("available") else 0,
                        fetched_at,
                        fetched_at,
                        int(u["id"]),
                    ),
                )
                updated += cur.rowcount
        if updated:
            invalidate_counts_cache()
    finally:
        conn.close()
    return updated


def mark_price_stock_attempted(product_ids: List[int], attempted_at: Optional[str] = None) -> int:
    """Records a refresh attempt (including failures) so the TTL can serve cache calmly."""
    ids = [int(i) for i in product_ids if i is not None]
    if not ids:
        return 0

    attempted_at = attempted_at or utc_now_iso()
    conn = get_db_connection()
    updated = 0
    try:
        with conn:
            for start in range(0, len(ids), _SQL_CHUNK):
                chunk = ids[start:start + _SQL_CHUNK]
                placeholders = ", ".join(["?"] * len(chunk))
                cur = conn.execute(
                    f"UPDATE products SET price_stock_attempted_at = ? WHERE id IN ({placeholders});",
                    [attempted_at] + chunk,
                )
                updated += cur.rowcount
    finally:
        conn.close()
    return updated


def get_price_stock_stats() -> Dict[str, Any]:
    """Summarises how many products carry live-refreshed price/stock and when."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS total FROM products;")
        total = cur.fetchone()["total"]
        cur.execute(
            "SELECT COUNT(*) AS refreshed FROM products WHERE price_stock_updated_at IS NOT NULL;"
        )
        refreshed = cur.fetchone()["refreshed"]
        cur.execute(
            "SELECT MAX(price_stock_updated_at) AS last_refresh FROM products;"
        )
        last_refresh = cur.fetchone()["last_refresh"]
        return {
            "total_products": total,
            "with_live_price_stock": refreshed,
            "with_cached_price_stock_only": max(total - refreshed, 0),
            "last_successful_refresh": str(last_refresh) if last_refresh else None,
        }
    finally:
        conn.close()


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


# ---------------------------------------------------------------------------
# Agent-friendly query recovery: stop words, AND -> OR relaxation, spell fixes
# ---------------------------------------------------------------------------


def _ensure_fts_vocab(conn: sqlite3.Connection) -> bool:
    """
    Lazily creates the FTS5 vocabulary table used for spelling suggestions.

    fts5vocab is a view over the FTS index and costs nothing to keep; creation
    is skipped silently on SQLite builds without the module (results still work,
    only typo suggestions are lost).
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS products_fts_vocab "
            "USING fts5vocab('products_fts', 'row')"
        )
        return True
    except sqlite3.Error as exc:  # noqa: BLE001 - suggestions are best-effort
        logger.debug("FTS vocabulary unavailable (%s); typo suggestions disabled.", exc)
        return False


def _fts_match_count(conn: sqlite3.Connection, expr: str) -> int:
    """Number of indexed rows matching an FTS5 expression."""
    if not expr:
        return 0
    row = conn.execute(
        "SELECT COUNT(1) FROM products_fts WHERE products_fts MATCH ?;", (expr,)
    ).fetchone()
    return int(row[0]) if row else 0


def _vocab_has_term(conn: sqlite3.Connection, term: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM products_fts_vocab WHERE term = ? LIMIT 1;", (term,)
    ).fetchone()
    return row is not None


def suggest_query_token(conn: sqlite3.Connection, token: str, limit: int = 3) -> List[str]:
    """Closest index vocabulary terms to a mistyped word-like token."""
    if not correctable(token):
        return []
    lowered = token.lower()
    if _vocab_has_term(conn, lowered):
        return []

    candidates: List[str] = []
    for prefix_len in (3, 2):
        prefix = lowered[:prefix_len]
        if len(prefix) < 2:
            break
        rows = conn.execute(
            "SELECT DISTINCT term FROM products_fts_vocab WHERE term LIKE ? LIMIT 4000;",
            (prefix + "%",),
        ).fetchall()
        candidates = [str(r[0]) for r in rows]
        if len(candidates) >= 5:
            break
    if not candidates:
        return []
    return difflib.get_close_matches(lowered, candidates, n=limit, cutoff=0.75)


def _fts_term_doc_count(conn: sqlite3.Connection, token: str) -> int:
    """Document frequency of one token in the FTS vocabulary."""
    row = conn.execute(
        "SELECT doc FROM products_fts_vocab WHERE term = ? LIMIT 1;", (token.lower(),)
    ).fetchone()
    return int(row[0]) if row and row[0] else 1


def _head_token(tokens: List[str]) -> Optional[str]:
    """
    The earliest token that names a taxonomy category ("battery", "motor", ...).

    The head term is the anchor of a natural-language component query. It must
    never be relaxed away; the modifiers around it may be.
    """
    try:
        taxonomy = get_taxonomy()
        category_tokens = set()
        for category in taxonomy.categories.values():
            category_tokens.add(category.id.replace("-", " "))
            category_tokens.update(category.detect_keywords)
        flat = {token for value in category_tokens for token in tokenize(value)}
        for token in tokens:
            if token in flat:
                return token
            if any(len(term) >= 4 and token.startswith(term) for term in flat):
                return token
    except Exception:  # noqa: BLE001 - head detection is best-effort
        return None
    return None


def _spec_like_token(token: str) -> bool:
    """Numeric/unit tokens are constraints; drop them only after plain modifiers."""
    if re.fullmatch(r"\d{1,2}", token):
        # A bare "8" is far more likely to be "8 hours" than "8V"; do not keep
        # it as a hard constraint after relaxation.
        return False
    return bool(re.search(r"\d", token)) or token.lower() in {
        "v", "a", "w", "mah", "ah", "rpm", "kgcm", "kg", "mm", "cm", "nm", "ohm",
    }


def _progressive_relaxation(
    conn: sqlite3.Connection,
    tokens: List[str],
    head: Optional[str],
) -> Tuple[Optional[List[str]], List[str]]:
    """
    Drops plain modifiers first, then numeric constraints, never the head term.

    This replaces the old all-or-nothing AND -> OR switch, which turned
    "battery to power an arduino for 8 hours" into 30,119 rows. "hours" must not
    survive as the last token: it would match capacitor lifetimes, not batteries.
    """
    remaining = list(tokens)
    dropped: List[str] = []

    while len(remaining) > 1:
        candidates = [t for t in remaining if t != head]
        if not candidates:
            break
        modifiers = [t for t in candidates if not _spec_like_token(t)]
        pool = modifiers or candidates
        drop = max(pool, key=lambda t: (_fts_term_doc_count(conn, t), t))
        remaining.remove(drop)
        dropped.append(drop)
        if _fts_match_count(conn, strict_fts_expr(remaining)) > 0:
            return remaining, dropped

    if remaining and _fts_match_count(conn, strict_fts_expr(remaining)) > 0:
        return remaining, dropped
    return None, dropped


def resolve_query_plan(query: Optional[str], conn: Optional[sqlite3.Connection] = None) -> QueryPlan:
    """
    Resolves how a free-text query should hit the FTS index.

    Order of preference (first that matches wins):
      1. strict    - every content token matches (precision)
      2. corrected - spell-corrected tokens match (typo recovery)
      3. corrected_relaxed / relaxed - any token matches (recall)
    The returned plan carries explicit warnings so callers can tell the agent
    what was actually searched.
    """
    plan = QueryPlan(original=query or "")
    syntax = detect_unsupported_syntax(query)
    if syntax:
        plan.mode = "unresolved"
        plan.tokens = content_tokens(query)[0]
        plan.effective_tokens = plan.tokens
        plan.operator_tokens = list(syntax["operators"])
        plan.warnings.append({
            "code": "unsupported_query_syntax",
            "message": syntax["message"],
            "operators": syntax["operators"],
            "characters": syntax["characters"],
        })
        return plan

    tokens, ignored = content_tokens(query)
    plan.tokens = tokens
    plan.ignored = ignored
    operator_tokens = [
        token for token in ignored if token.lower() in ("or", "and", "not")
    ]
    plan.operator_tokens = operator_tokens
    head = _head_token(tokens)
    plan.head_token = head or ""
    if not tokens:
        return plan

    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    try:
        strict_count = _fts_match_count(conn, strict_fts_expr(tokens))
        if strict_count > 0:
            plan.mode = "strict"
            plan.effective_tokens = tokens
            if operator_tokens:
                plan.warnings.append(_operator_warning(plan, operator_tokens))
            if head and len(tokens) > 1:
                head_count = _fts_match_count(conn, strict_fts_expr([head]))
                plan.head_match_count = head_count
                if strict_count <= 20 and head_count >= 50 and head_count >= strict_count * 20:
                    plan.warnings.append({
                        "code": "query_narrowed",
                        "message": (
                            f"Only {strict_count} listing(s) matched all query terms, but "
                            f"{head_count} match '{head}' alone. This may be over-narrow: drop a "
                            "modifier or add spec filters to narrow deliberately."
                        ),
                        "head_token": head,
                        "head_matches": head_count,
                        "query_effective": plan.effective_query,
                    })
            return plan

        if not _ensure_fts_vocab(conn):
            # No vocabulary: relax token-wise so natural-language queries still work.
            if len(tokens) > 1 and _fts_match_count(conn, relaxed_fts_expr(tokens)) > 0:
                plan.mode = "relaxed"
                plan.effective_tokens = tokens
                plan.match_mode = "any_terms"
                plan.warnings.append(_relaxed_warning(plan))
                return plan
            plan.mode = "unresolved"
            plan.effective_tokens = tokens
            return plan

        corrections: Dict[str, str] = {}
        for token in tokens:
            fixes = suggest_query_token(conn, token, limit=1)
            if fixes:
                corrections[token] = fixes[0]

        if corrections:
            corrected = [corrections.get(t, t) for t in tokens]
            plan.corrections = corrections
            if _fts_match_count(conn, strict_fts_expr(corrected)) > 0:
                plan.mode = "corrected"
                plan.effective_tokens = corrected
                plan.warnings.append(_corrected_warning(plan))
                if operator_tokens:
                    plan.warnings.append(_operator_warning(plan, operator_tokens))
                return plan
            if len(corrected) > 1 and _fts_match_count(conn, relaxed_fts_expr(corrected)) > 0:
                plan.mode = "corrected_relaxed"
                plan.effective_tokens = corrected
                plan.match_mode = "any_terms"
                plan.warnings.append(_corrected_warning(plan))
                plan.warnings.append(_relaxed_warning(plan))
                return plan

        # Progressive relaxation: drop the most generic token first, so a
        # natural-language modifier ("robot", "arduino") does not collapse the
        # result set to a near-empty list.
        if len(tokens) > 1:
            subset, dropped = _progressive_relaxation(conn, tokens, head)
            if subset:
                plan.mode = "relaxed"
                plan.effective_tokens = subset
                plan.dropped_tokens = dropped
                plan.match_mode = "all_terms"
                plan.warnings.append(_relaxed_warning(plan))
                if operator_tokens:
                    plan.warnings.append(_operator_warning(plan, operator_tokens))
                return plan

        # Last resort: any-term matching. The MCP layer flags this as
        # low-confidence because a single matching token is not a search.
        if len(tokens) > 1 and _fts_match_count(conn, relaxed_fts_expr(tokens)) > 0:
            plan.mode = "relaxed"
            plan.effective_tokens = tokens
            plan.match_mode = "any_terms"
            plan.warnings.append(_relaxed_warning(plan))
            if operator_tokens:
                plan.warnings.append(_operator_warning(plan, operator_tokens))
            return plan

        plan.mode = "unresolved"
        plan.effective_tokens = tokens
        for token, fix in corrections.items():
            variant = [fix if t == token else t for t in tokens]
            candidate = " ".join(variant)
            if candidate not in plan.suggestions:
                plan.suggestions.append(candidate)
        plan.warnings.append({
            "code": "query_unresolved",
            "message": (
                "None of the query terms matched the catalog index. Check the spelling or "
                "broaden the query; data.query_suggestions lists close alternatives when known."
            ),
            "suggestions": list(plan.suggestions),
        })
        return plan
    finally:
        if own_conn:
            conn.close()


def _corrected_warning(plan: QueryPlan) -> Dict[str, Any]:
    fixes = ", ".join(f"'{src}' -> '{dst}'" for src, dst in plan.corrections.items())
    return {
        "code": "query_spelling_corrected",
        "message": f"No listing matched the query spelling; corrected {fixes}.",
        "corrections": dict(plan.corrections),
        "query_effective": plan.effective_query,
    }


def _operator_warning(plan: QueryPlan, operators: List[str]) -> Dict[str, Any]:
    return {
        "code": "query_operator_dropped",
        "message": (
            f"The word(s) {operators} were treated as filler, not as boolean operators. "
            "This server does not implement OR/AND/NOT; issue separate searches instead."
        ),
        "operators": operators,
        "query_effective": plan.effective_query,
    }


def _relaxed_warning(plan: QueryPlan) -> Dict[str, Any]:
    if plan.match_mode == "any_terms":
        message = (
            "No listing matched the query terms together; results match any single term and "
            "are ranked by relevance. Treat this as a broad discovery result, not an exact match."
        )
    else:
        dropped = ", ".join(plan.dropped_tokens) or "none"
        message = (
            "No listing matched every query term; the most generic term(s) were dropped "
            f"(dropped: {dropped}) and the remaining terms are matched together. "
            "Narrow the query or add spec filters for exact matches."
        )
    return {
        "code": "query_relaxed",
        "message": message,
        "query_effective": plan.effective_query,
        "match_mode": plan.match_mode,
        "dropped_tokens": list(plan.dropped_tokens),
    }


# ---------------------------------------------------------------------------
# MPN / SKU resolution (separator-insensitive, with did-you-mean suggestions)
# ---------------------------------------------------------------------------

_IDENT_SEPARATORS = re.compile(r"[^A-Za-z0-9]")

# SQL expression that normalizes stored identifiers the same way as
# normalize_identifier() for separator-insensitive equality lookups.
_NORM_COL_TEMPLATE = (
    "replace(replace(replace(upper(trim(coalesce({col}, ''))), '-', ''), ' ', ''), '_', '')"
)


def normalize_identifier(value: Optional[str]) -> str:
    """Uppercases and removes separators so 'MPU-6050' == 'mpu 6050' == 'MPU6050'."""
    return _IDENT_SEPARATORS.sub("", value or "").upper()


def find_products_by_mpn(needle: str, limit: int = 20, offset: int = 0) -> Tuple[List[sqlite3.Row], int]:
    """
    Exact MPN/vendor-SKU lookup ignoring case, spaces, hyphens and underscores.

    Returns (rows, total) where rows carry the same columns as the old
    search_by_mpn listing query.
    """
    normalized = normalize_identifier(needle)
    if not normalized:
        return [], 0

    mpn_expr = _NORM_COL_TEMPLATE.format(col="mpn")
    sku_expr = _NORM_COL_TEMPLATE.format(col="sku")
    where_sql = f"({mpn_expr} = ? OR {sku_expr} = ?)"

    conn = get_db_connection()
    try:
        total = int(conn.execute(
            f"SELECT COUNT(1) FROM products WHERE {where_sql};", (normalized, normalized)
        ).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT id, vendor, vendor_name, title, price, available, product_url, sku, mpn,
                   category, price_stock_source, price_stock_updated_at
            FROM products
            WHERE {where_sql}
            ORDER BY available DESC, price ASC
            LIMIT ? OFFSET ?;
            """,
            (normalized, normalized, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return rows, total
    finally:
        conn.close()


def mpn_match_stats(needle: str, sample_limit: int = 8) -> Dict[str, Any]:
    """
    Ambiguity statistics for an MPN/SKU match.

    A single MPN value shared by hundreds of unrelated titles ("ESP32") is a
    family token, not a part number; callers should not present it as an exact
    lookup.
    """
    normalized = normalize_identifier(needle)
    if not normalized:
        return {"total": 0, "distinct_titles": 0, "distinct_vendors": 0, "sample_titles": []}

    mpn_expr = _NORM_COL_TEMPLATE.format(col="mpn")
    sku_expr = _NORM_COL_TEMPLATE.format(col="sku")
    where_sql = f"({mpn_expr} = ? OR {sku_expr} = ?)"
    conn = get_db_connection()
    try:
        total = int(conn.execute(
            f"SELECT COUNT(1) FROM products WHERE {where_sql};", (normalized, normalized)
        ).fetchone()[0])
        distinct_titles = int(conn.execute(
            f"SELECT COUNT(DISTINCT title) FROM products WHERE {where_sql};",
            (normalized, normalized),
        ).fetchone()[0])
        distinct_vendors = int(conn.execute(
            f"SELECT COUNT(DISTINCT vendor) FROM products WHERE {where_sql};",
            (normalized, normalized),
        ).fetchone()[0])
        samples = [
            str(r[0]) for r in conn.execute(
                f"SELECT DISTINCT title FROM products WHERE {where_sql} LIMIT ?;",
                (normalized, normalized, int(sample_limit)),
            ).fetchall()
        ]
        return {
            "total": total,
            "distinct_titles": distinct_titles,
            "distinct_vendors": distinct_vendors,
            "sample_titles": samples,
        }
    finally:
        conn.close()


def suggest_mpns(needle: str, limit: int = 5, cutoff: float = 0.72) -> List[str]:
    """Closest catalog MPNs to a mistyped/partial part number (separator-insensitive)."""
    normalized = normalize_identifier(needle)
    if len(normalized) < 3:
        return []

    mpn_expr = _NORM_COL_TEMPLATE.format(col="mpn")
    conn = get_db_connection()
    try:
        candidates: List[str] = []
        for prefix_len in (3, 2):
            prefix = normalized[:prefix_len]
            rows = conn.execute(
                f"""
                SELECT DISTINCT mpn FROM products
                WHERE mpn IS NOT NULL AND mpn != '' AND {mpn_expr} LIKE ?
                LIMIT 4000;
                """,
                (prefix + "%",),
            ).fetchall()
            candidates = [str(r[0]) for r in rows]
            if len(candidates) >= 5:
                break
        if not candidates:
            return []

        scored: List[Tuple[float, str]] = []
        for candidate in candidates:
            cand_norm = normalize_identifier(candidate)
            if not cand_norm:
                continue
            ratio = difflib.SequenceMatcher(None, normalized, cand_norm).ratio()
            if ratio >= cutoff:
                scored.append((ratio, candidate))
        scored.sort(key=lambda item: -item[0])

        suggestions: List[str] = []
        for _, candidate in scored:
            if candidate not in suggestions:
                suggestions.append(candidate)
            if len(suggestions) >= limit:
                break
        return suggestions
    finally:
        conn.close()


def _browse_filter_clauses(
    category: Optional[str],
    vendor: Optional[str],
    in_stock_only: bool,
    spec_filters: Optional[Dict[str, Any]],
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
) -> Tuple[List[str], List[Any], List[str], List[Any]]:
    """Shared WHERE/JOIN builder for category browsing and its total count."""
    clauses: List[str] = []
    where_params: List[Any] = []

    if category:
        clauses.append("category_canonical = ?")
        where_params.append(category)
    if vendor:
        clauses.append("vendor = ?")
        where_params.append(vendor)
    if in_stock_only:
        clauses.append("available = 1")
    if price_min is not None:
        clauses.append("price >= ?")
        where_params.append(float(price_min))
    if price_max is not None:
        clauses.append("price <= ?")
        where_params.append(float(price_max))

    joins: List[str] = []
    join_params: List[Any] = []
    if spec_filters:
        conn = get_db_connection()
        try:
            advertised = load_catalog_specs(conn)
        finally:
            conn.close()
        norm_filters, error = parse_and_validate_filters(spec_filters, advertised)
        if error:
            raise ValueError(error["message"])
        joins, join_params, spec_where, spec_where_params = build_spec_clauses(
            norm_filters, advertised
        )
        clauses.extend(spec_where)
        where_params.extend(spec_where_params)
    return joins, join_params, clauses, where_params


def list_products_by_category(
    category: Optional[str] = None,
    vendor: Optional[str] = None,
    in_stock_only: bool = False,
    limit: int = 70,
    offset: int = 0,
    spec_filters: Optional[Dict[str, Any]] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
) -> List[UnifiedProduct]:
    """
    Lists catalog entries for browsing (no text query).

    Used by the category pages: the most relevant, best-priced listings of a
    category so a user can browse without typing anything. `spec_filters`
    applies the same shared spec-attribute filters as the search path.
    """
    joins, join_params, clauses, where_params = _browse_filter_clauses(
        category, vendor, in_stock_only, spec_filters,
        price_min=price_min, price_max=price_max,
    )
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params: List[Any] = join_params + where_params
    params.extend([max(1, int(limit)), max(0, int(offset))])

    conn = get_db_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT p.* FROM products p
            {" ".join(joins)}
            {where}
            ORDER BY p.available DESC, p.price ASC, p.id ASC
            LIMIT ? OFFSET ?;
            """,
            params,
        ).fetchall()
        return [row_to_unified_product(r) for r in rows]
    finally:
        conn.close()


def count_products_by_category(
    category: Optional[str] = None,
    vendor: Optional[str] = None,
    in_stock_only: bool = False,
    spec_filters: Optional[Dict[str, Any]] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
) -> int:
    """Total listings in a browse filter (drives the pager's total count)."""
    joins, join_params, clauses, where_params = _browse_filter_clauses(
        category, vendor, in_stock_only, spec_filters,
        price_min=price_min, price_max=price_max,
    )
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = get_db_connection()
    try:
        row = conn.execute(
            f"SELECT COUNT(DISTINCT p.id) FROM products p {' '.join(joins)} {where};",
            join_params + where_params,
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()



def _prepare_search_query(
    conn: sqlite3.Connection,
    query: str,
    vendor: Optional[str],
    in_stock_only: bool,
    category: Optional[str],
    spec_filters: Optional[Dict[str, Any]],
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Resolves a text query into the exact SQL fragments shared by search and count.

    Returns None when the query is empty or nothing in the index resembles it
    (callers must return [] / 0 instead of silently matching the whole catalog).
    """
    clean_q = query.strip() if query else ""
    if not clean_q:
        return None

    plan = resolve_query_plan(clean_q, conn=conn)
    if not plan.resolved or not plan.effective_tokens:
        return None

    # Relaxed/corrected plans match any term; strict plans use the wider
    # variation set (all terms, concatenation, bigram merges).
    if plan.is_relaxed:
        fts_query = relaxed_fts_expr(plan.effective_tokens)
    else:
        fts_query = " OR ".join(fts_variations(plan.effective_tokens))

    # Spec-attribute filters (voltage, rpm, capacity_mah, ...) reuse the exact
    # same builder the MCP server uses, so a filter means the same thing in both.
    spec_joins: List[str] = []
    spec_join_params: List[Any] = []
    spec_where: List[str] = []
    spec_where_params: List[Any] = []
    if spec_filters:
        advertised = load_catalog_specs(conn)
        norm_filters, error = parse_and_validate_filters(spec_filters, advertised)
        if error:
            raise ValueError(error["message"])
        spec_joins, spec_join_params, spec_where, spec_where_params = build_spec_clauses(
            norm_filters, advertised
        )

    where_clauses = ["products_fts MATCH ?"]
    where_params: List[Any] = [fts_query]
    if vendor:
        where_clauses.append("p.vendor = ?")
        where_params.append(vendor)
    if in_stock_only:
        where_clauses.append("p.available = 1")
    if category:
        where_clauses.append("p.category_canonical = ?")
        where_params.append(category)
    if price_min is not None:
        where_clauses.append("p.price >= ?")
        where_params.append(float(price_min))
    if price_max is not None:
        where_clauses.append("p.price <= ?")
        where_params.append(float(price_max))
    where_clauses.extend(spec_where)

    return {
        "clean_q": clean_q,
        "plan": plan,
        "fts_query": fts_query,
        "where_clauses": where_clauses,
        "where_params": where_params,
        "spec_joins": spec_joins,
        "spec_join_params": spec_join_params,
        "spec_where": spec_where,
        "spec_where_params": spec_where_params,
    }


def search_products(
    query: str,
    limit: int = 50,
    offset: int = 0,
    vendor: Optional[str] = None,
    in_stock_only: bool = False,
    category: Optional[str] = None,
    spec_filters: Optional[Dict[str, Any]] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
) -> List[UnifiedProduct]:
    """
    Performs search using FTS5 with fallback to LIKE pattern matching.

    Text queries go through `resolve_query_plan` first: stop words are dropped,
    natural-language requests fall back from AND to OR matching, and mistyped
    terms are spell-corrected against the index vocabulary. When even that
    fails the search returns [] instead of the whole catalog.

    `category` filters on the canonical category slug (see
    `config/taxonomy.yaml`). `spec_filters` applies the shared spec-attribute
    filters (voltage, rpm, capacity_mah, ...) from `src/spec/filters.py`.
    """
    conn = get_db_connection()
    results: List[UnifiedProduct] = []
    try:
        prepared = _prepare_search_query(
            conn, query, vendor, in_stock_only, category, spec_filters,
            price_min=price_min, price_max=price_max,
        )
        if prepared is None:
            return []

        sql = f"""
            SELECT p.* FROM products p
            JOIN products_fts fts ON p.id = fts.rowid
            {" ".join(prepared["spec_joins"])}
            WHERE {" AND ".join(prepared["where_clauses"])}
            ORDER BY rank
            LIMIT ? OFFSET ?;
        """
        # Parameter order follows the SQL text: join params, then WHERE params.
        params: List[Any] = (
            prepared["spec_join_params"]
            + prepared["where_params"]
            + prepared["spec_where_params"]
        )
        params.extend([limit, offset])

        cursor = conn.cursor()
        try:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            rows = _search_products_like(
                cursor,
                prepared["clean_q"],
                (limit, offset),
                vendor,
                in_stock_only,
                category,
                spec_joins=prepared["spec_joins"],
                spec_join_params=prepared["spec_join_params"],
                spec_where=prepared["spec_where"],
                spec_where_params=prepared["spec_where_params"],
            )

        for r in rows:
            results.append(row_to_unified_product(r))
    finally:
        conn.close()

    return results


def count_search_products(
    query: str,
    vendor: Optional[str] = None,
    in_stock_only: bool = False,
    category: Optional[str] = None,
    spec_filters: Optional[Dict[str, Any]] = None,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
) -> int:
    """Total listings matching a search (used for pager totals, not just the page)."""
    conn = get_db_connection()
    try:
        prepared = _prepare_search_query(
            conn, query, vendor, in_stock_only, category, spec_filters,
            price_min=price_min, price_max=price_max,
        )
        if prepared is None:
            return 0

        sql = f"""
            SELECT COUNT(DISTINCT p.id) FROM products p
            JOIN products_fts fts ON p.id = fts.rowid
            {" ".join(prepared["spec_joins"])}
            WHERE {" AND ".join(prepared["where_clauses"])};
        """
        params: List[Any] = (
            prepared["spec_join_params"]
            + prepared["where_params"]
            + prepared["spec_where_params"]
        )
        try:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0
        except sqlite3.OperationalError:
            like = f"%{prepared['clean_q']}%"
            like_where = [
                "(p.title LIKE ? OR p.normalized_title LIKE ? OR p.mpn LIKE ? OR p.sku LIKE ?)"
            ]
            like_params: List[Any] = [like, like, like, like]
            if vendor:
                like_where.append("p.vendor = ?")
                like_params.append(vendor)
            if in_stock_only:
                like_where.append("p.available = 1")
            if category:
                like_where.append("p.category_canonical = ?")
                like_params.append(category)
            if price_min is not None:
                like_where.append("p.price >= ?")
                like_params.append(float(price_min))
            if price_max is not None:
                like_where.append("p.price <= ?")
                like_params.append(float(price_max))
            like_where.extend(prepared["spec_where"])
            like_params.extend(prepared["spec_where_params"])
            row = conn.execute(
                f"SELECT COUNT(DISTINCT p.id) FROM products p "
                f"{' '.join(prepared['spec_joins'])} "
                f"WHERE {' AND '.join(like_where)};",
                prepared["spec_join_params"] + like_params,
            ).fetchone()
            return int(row[0]) if row else 0
    finally:
        conn.close()



def _search_products_like(
    cursor: sqlite3.Cursor,
    clean_q: str,
    page: Tuple[int, int],
    vendor: Optional[str],
    in_stock_only: bool,
    category: Optional[str] = None,
    spec_joins: Optional[List[str]] = None,
    spec_join_params: Optional[List[Any]] = None,
    spec_where: Optional[List[str]] = None,
    spec_where_params: Optional[List[Any]] = None,
) -> List[sqlite3.Row]:
    """LIKE fallback used when FTS5 rejects a query (malformed token, etc.)."""
    like_clauses = ["(p.title LIKE ? OR p.normalized_title LIKE ? OR p.mpn LIKE ? OR p.sku LIKE ?)"]
    like_pat = f"%{clean_q}%"
    where_params: List[Any] = [like_pat, like_pat, like_pat, like_pat]
    if vendor:
        like_clauses.append("p.vendor = ?")
        where_params.append(vendor)
    if in_stock_only:
        like_clauses.append("p.available = 1")
    if category:
        like_clauses.append("p.category_canonical = ?")
        where_params.append(category)

    where_clauses = list(spec_where or [])
    where_params.extend(spec_where_params or [])

    sql_like = f"""
        SELECT p.* FROM products p
        {" ".join(spec_joins or [])}
        WHERE {" AND ".join(like_clauses + where_clauses)}
        ORDER BY p.available DESC, p.price ASC
        LIMIT ? OFFSET ?;
    """
    params: List[Any] = list(spec_join_params or []) + where_params
    params.extend(list(page))
    cursor.execute(sql_like, params)
    return cursor.fetchall()

def apply_vendor_gst(
    vendor: str,
    rate: float,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    One-off/idempotent conversion of a vendor's stored ex-GST prices.

    Rows ingested after the adapter learned about GST already carry
    `extra.gst_rate`; those are skipped. The original amount is preserved in
    `extra.price_ex_gst`, and the refresh marker is cleared so the next search
    re-verifies against the vendor.
    """
    rate = max(0.0, float(rate or 0.0))
    if rate <= 0:
        return {"vendor": vendor, "rate": rate, "updated": 0, "skipped": 0, "dry_run": dry_run}

    conn = get_db_connection()
    updated = 0
    skipped = 0
    try:
        rows = conn.execute(
            "SELECT id, price, raw_json FROM products WHERE vendor = ? AND price > 0;",
            (vendor,),
        ).fetchall()

        with conn:
            for row in rows:
                try:
                    extra = json.loads(row["raw_json"]) if row["raw_json"] else {}
                except (TypeError, ValueError):
                    extra = {}
                if not isinstance(extra, dict):
                    extra = {}
                if "gst_rate" in extra:
                    skipped += 1
                    continue

                base_price = float(row["price"])
                new_price = round(base_price * (1.0 + rate), 2)
                extra["price_ex_gst"] = base_price
                extra["gst_rate"] = rate
                extra["price_note"] = (
                    f"₹{base_price:,.2f} + {rate * 100:.0f}% GST "
                    f"(tax-inclusive price shown)"
                )
                if not dry_run:
                    conn.execute(
                        """
                        UPDATE products
                        SET price = ?,
                            raw_json = ?,
                            price_stock_attempted_at = NULL
                        WHERE id = ?;
                        """,
                        (new_price, json.dumps(extra), row["id"]),
                    )
                updated += 1
    finally:
        conn.close()
    return {
        "vendor": vendor,
        "rate": rate,
        "updated": updated,
        "skipped": skipped,
        "dry_run": dry_run,
    }


def get_db_stats() -> Dict[str, Any]:
    """Returns database summary statistics and vendor checkpoints."""
    cached = _cache_get(_stats_cache)
    if cached is not None:
        return cached

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

        stats = {
            "total_products": total_products,
            "in_stock_products": in_stock_products,
            "vendors": vendors,
            "checkpoints": checkpoints,
            "robu_registry": get_robu_registry_stats(),
            "price_stock": get_price_stock_stats()
        }
        _cache_set(_stats_cache, stats)
        return stats
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Canonical categories: taxonomy metadata, facets and filtering
# ---------------------------------------------------------------------------

# The category/vendor counts are read on every page render but only change
# when the catalog is re-ingested, so they are cached for a short while.
_COUNTS_CACHE_TTL_SECONDS = 30
_counts_cache: Dict[str, Any] = {"at": 0.0, "categories": None, "vendors": None}


def _catalog_counts(vendor: Optional[str] = None) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    """Returns (category_counts, vendor_counts) using a short-lived cache."""
    now = time.monotonic()
    if (
        vendor is None
        and _counts_cache["categories"] is not None
        and (now - _counts_cache["at"]) < _COUNTS_CACHE_TTL_SECONDS
    ):
        return _counts_cache["categories"], _counts_cache["vendors"]

    categories = get_category_counts(vendor=vendor)
    vendors = get_vendor_counts(category=None)
    if vendor is None:
        _counts_cache["at"] = now
        _counts_cache["categories"] = categories
        _counts_cache["vendors"] = vendors
    return categories, vendors


def sync_category_taxonomy(conn: Optional[sqlite3.Connection] = None) -> int:
    """Writes the taxonomy definition into `category_taxonomy`.

    Keeps the UI's labels/icons in the database so the web layer does not need
    to load the YAML on every request. Returns the number of categories written.
    """
    taxonomy = get_taxonomy()
    own = conn is None
    conn = conn or get_db_connection()
    try:
        with conn:
            conn.executemany(
                """
                INSERT INTO category_taxonomy (slug, label, icon, description, sort_order, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    label = excluded.label,
                    icon = excluded.icon,
                    description = excluded.description,
                    sort_order = excluded.sort_order,
                    updated_at = excluded.updated_at;
                """,
                [
                    (c.id, c.display, c.icon, c.description, order, utc_now_iso())
                    for order, c in enumerate(taxonomy.categories.values())
                ],
            )
        return len(taxonomy.categories)
    finally:
        if own:
            conn.close()


def get_categories(vendor: Optional[str] = None, with_counts: bool = True) -> List[Dict[str, Any]]:
    """
    Returns the canonical taxonomy, optionally with live product counts.

    Counts come from `products.category_canonical`, so they always reflect what
    is actually in the catalog rather than a hard-coded list.
    """
    taxonomy = get_taxonomy()
    if with_counts and vendor is None:
        cached = _cache_get(_categories_cache)
        if cached is not None:
            return cached

    counts: Dict[str, Dict[str, int]] = {}
    if with_counts:
        conn = get_db_connection()
        try:
            if vendor:
                rows = conn.execute(
                    """
                    SELECT category_canonical AS slug,
                           COUNT(*) AS total,
                           SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS in_stock
                    FROM products
                    WHERE category_canonical IS NOT NULL AND vendor = ?
                    GROUP BY category_canonical;
                    """,
                    (vendor,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT category_canonical AS slug,
                           COUNT(*) AS total,
                           SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS in_stock
                    FROM products
                    WHERE category_canonical IS NOT NULL
                    GROUP BY category_canonical;
                    """
                ).fetchall()
            counts = {
                r["slug"]: {"total": r["total"], "in_stock": r["in_stock"] or 0}
                for r in rows
            }
        finally:
            conn.close()

    out: List[Dict[str, Any]] = []
    for cat in taxonomy.categories.values():
        stats = counts.get(cat.id, {"total": 0, "in_stock": 0})
        out.append(
            {
                "slug": cat.id,
                "label": cat.display,
                "icon": cat.icon,
                "description": cat.description,
                "count": stats["total"],
                "in_stock": stats["in_stock"],
                # The spec attributes that can be filtered on for this category
                # (config/taxonomy.yaml -> attributes).
                "filters": [
                    {
                        "key": attr.key,
                        "display_name": attr.display_name,
                        "type": attr.type,
                        "unit": attr.unit,
                        "values": attr.values,
                        "range": list(attr.range) if attr.range else None,
                    }
                    for attr in cat.attributes.values()
                ],
            }
        )
    if with_counts and vendor is None:
        _cache_set(_categories_cache, out)
    return out


def get_category_counts(vendor: Optional[str] = None) -> Dict[str, int]:
    """Returns {category_slug: product_count} for the whole catalog."""
    conn = get_db_connection()
    try:
        if vendor:
            rows = conn.execute(
                """
                SELECT category_canonical AS slug, COUNT(*) AS c
                FROM products
                WHERE category_canonical IS NOT NULL AND vendor = ?
                GROUP BY category_canonical;
                """,
                (vendor,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT category_canonical AS slug, COUNT(*) AS c
                FROM products
                WHERE category_canonical IS NOT NULL
                GROUP BY category_canonical;
                """
            ).fetchall()
        return {r["slug"]: r["c"] for r in rows}
    finally:
        conn.close()


def get_vendor_counts(category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns per-vendor counts, optionally scoped to one category."""
    if category is None:
        cached = _cache_get(_vendor_counts_cache)
        if cached is not None:
            return cached

    conn = get_db_connection()
    try:
        if category:
            rows = conn.execute(
                """
                SELECT vendor, vendor_name, COUNT(*) AS total,
                       SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS in_stock
                FROM products
                WHERE category_canonical = ?
                GROUP BY vendor, vendor_name
                ORDER BY total DESC;
                """,
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT vendor, vendor_name, COUNT(*) AS total,
                       SUM(CASE WHEN available = 1 THEN 1 ELSE 0 END) AS in_stock
                FROM products
                GROUP BY vendor, vendor_name
                ORDER BY total DESC;
                """
            ).fetchall()
        result = [
            {
                "vendor": r["vendor"],
                "vendor_name": r["vendor_name"],
                "count": r["total"],
                "in_stock": r["in_stock"] or 0,
            }
            for r in rows
        ]
        if category is None:
            _cache_set(_vendor_counts_cache, result)
        return result
    finally:
        conn.close()

def backfill_categories(conn: Optional[sqlite3.Connection] = None, batch_size: int = 5000) -> Dict[str, int]:
    """
    Maps every product onto a canonical category and stores the result.

    Uses `products.category` (vendor string) first and the product title as a
    fallback. Idempotent: rerunning it simply re-applies the current taxonomy.
    """
    from src.spec.taxonomy_loader import classify_product

    own = conn is None
    conn = conn or get_db_connection()
    stats = {"scanned": 0, "updated": 0, "unmatched": 0}
    try:
        cursor = conn.cursor()
        last_id = 0
        while True:
            rows = cursor.execute(
                """
                SELECT id, category, title, spec_category
                FROM products
                WHERE id > ?
                ORDER BY id
                LIMIT ?;
                """,
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            last_id = rows[-1]["id"]

            updates: List[Tuple[str, int]] = []
            mapping: Dict[str, Tuple[str, str]] = {}
            for r in rows:
                slug, source, _score = classify_product(
                    r["category"], r["title"], spec_category=r["spec_category"]
                )
                slug = slug or "other"
                updates.append((slug, r["id"]))
                mapping.setdefault(r["category"] or "", (slug, source))
                stats["scanned"] += 1
                if slug == "other":
                    stats["unmatched"] += 1

            with conn:
                conn.executemany(
                    "UPDATE products SET category_canonical = ? WHERE id = ?;",
                    updates,
                )
                conn.executemany(
                    """
                    INSERT INTO category_map (raw_category, category_slug, matched_keyword, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(raw_category) DO UPDATE SET
                        category_slug = excluded.category_slug,
                        matched_keyword = excluded.matched_keyword,
                        updated_at = excluded.updated_at;
                    """,
                    [
                        (raw, slug, keyword, utc_now_iso())
                        for raw, (slug, keyword) in mapping.items()
                    ],
                )
            stats["updated"] += len(rows)
        sync_category_taxonomy(conn)
        return stats
    finally:
        if own:
            conn.close()


# ---------------------------------------------------------------------------
# Search suggestions
# ---------------------------------------------------------------------------

def _suggest_prefix_expr(prefix: str) -> Optional[str]:
    """Builds a safe FTS5 prefix expression for a single token."""
    token = re.sub(r"[^A-Za-z0-9]", "", prefix or "").lower()
    if len(token) < 2:
        return None
    return f'"{token}"*'


def suggest_products(
    prefix: str,
    limit: int = 8,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Type-ahead suggestions for a partially typed query.

    Combines three signals into one ranked list:
      * part numbers (MPN) that start with the prefix,
      * product titles that contain a prefix-matching token,
      * canonical categories whose label matches.

    Uses the FTS5 prefix index, so it stays fast on the 100k+ row catalog.
    """
    taxonomy = get_taxonomy()
    expr = _suggest_prefix_expr(prefix)
    if expr is None:
        return []

    conn = get_db_connection()
    try:
        params: List[Any] = [expr]
        cat_clause = ""
        if category:
            cat_clause = "AND p.category_canonical = ?"
            params.append(category)

        rows = conn.execute(
            f"""
            SELECT p.mpn, p.title, p.normalized_title, p.vendor, p.vendor_name,
                   p.price, p.available, p.category_canonical, p.product_url
            FROM products_fts f
            JOIN products p ON p.id = f.rowid
            WHERE products_fts MATCH ? {cat_clause}
            LIMIT 1200;
            """,
            params,
        ).fetchall()
    finally:
        conn.close()

    prefix_lc = re.sub(r"[^A-Za-z0-9]", "", prefix or "").lower()
    if not prefix_lc:
        return []

    suggestions: List[Dict[str, Any]] = []
    seen_mpn = set()
    seen_title = set()

    # Part numbers first: users usually search by MPN.
    for r in rows:
        mpn = (r["mpn"] or "").strip()
        if not mpn or mpn in seen_mpn:
            continue
        normalized = re.sub(r"[^A-Za-z0-9]", "", mpn).lower()
        if not normalized.startswith(prefix_lc):
            continue
        seen_mpn.add(mpn)
        suggestions.append(
            {
                "type": "mpn",
                "value": mpn,
                "label": mpn,
                "hint": r["title"] if r["title"] else None,
                "vendor": r["vendor_name"] or r["vendor"],
                "price": r["price"],
                "available": bool(r["available"]),
                "category": r["category_canonical"],
                "url": r["product_url"],
            }
        )
        if len(suggestions) >= min(4, limit):
            break

    # Then representative product titles, best match first.
    title_hits = []
    for r in rows:
        title = (r["title"] or "").strip()
        if not title or title in seen_title:
            continue
        seen_title.add(title)
        title_hits.append((_title_relevance(title, prefix_lc), title, r))

    title_hits.sort(key=lambda item: (-item[0], len(item[1])))

    for score, title, r in title_hits:
        suggestions.append(
            {
                "type": "product",
                "value": title,
                "label": title,
                "hint": r["mpn"],
                "vendor": r["vendor_name"] or r["vendor"],
                "price": r["price"],
                "available": bool(r["available"]),
                "category": r["category_canonical"],
                "url": r["product_url"],
            }
        )
        if len(suggestions) >= max(1, limit - 2):
            break

    # Finally category labels, so "res" also offers the Resistors category.
    if len(suggestions) < limit:
        for cat in taxonomy.categories.values():
            if cat.id in (category,):
                continue
            if prefix_lc and prefix_lc not in re.sub(r"[^a-z0-9]", "", cat.display.lower()):
                continue
            suggestions.append(
                {
                    "type": "category",
                    "value": cat.id,
                    "label": cat.display,
                    "hint": cat.description,
                    "vendor": None,
                    "price": None,
                    "available": None,
                    "category": cat.id,
                    "url": None,
                }
            )
            if len(suggestions) >= limit:
                break

    return suggestions


def _title_relevance(title: str, prefix_lc: str) -> float:
    """Heuristic score for how well a title matches the typed prefix."""
    tokens = re.findall(r"[A-Za-z0-9]+", title.lower())
    score = 0.0
    for index, token in enumerate(tokens):
        if token.startswith(prefix_lc):
            # Earlier matches are stronger; a whole-word match is stronger still.
            score += max(0.0, 3.0 - index * 0.4)
            if token == prefix_lc:
                score += 1.5
            if index == 0:
                score += 1.5
    # Prefer titles that are short and start with the match.
    score -= len(title) / 400.0
    return score


def record_search_query(query: str) -> None:
    """Logs a search so the UI can surface popular queries."""
    clean = (query or "").strip().lower()
    if len(clean) < 3:
        return
    conn = get_db_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO search_queries (query, count, last_seen)
                VALUES (?, 1, ?)
                ON CONFLICT(query) DO UPDATE SET
                    count = count + 1,
                    last_seen = excluded.last_seen;
                """,
                (clean, utc_now_iso()),
            )
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()


def get_popular_queries(limit: int = 8, min_count: int = 2, max_length: int = 28) -> List[str]:
    """Returns the most searched queries, most popular first.

    Long natural-language queries are skipped: they make terrible chips in the
    "popular searches" row.
    """
    conn = get_db_connection()
    try:
        rows = conn.execute(
            """
            SELECT query FROM search_queries
            WHERE count >= ? AND LENGTH(query) <= ?
            ORDER BY count DESC, last_seen DESC
            LIMIT ?;
            """,
            (min_count, max_length, limit),
        ).fetchall()
        return [r["query"] for r in rows]
    finally:
        conn.close()


def get_category_map(raw_category: str) -> Optional[str]:
    """Returns the canonical slug a vendor category maps to (if known)."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT category_slug FROM category_map WHERE raw_category = ?;",
            (raw_category or "",),
        ).fetchone()
        return row["category_slug"] if row else None
    finally:
        conn.close()
