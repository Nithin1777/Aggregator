"""
Taxonomy Backfill Script.
1. Sync spec_definitions from taxonomy.yaml (upsert; delete removed attributes, log count).
2. Classify every product in catalog (products.spec_category).
3. Extract specs for classified products (DELETE-THEN-INSERT per product, preserving manual
   and promoted sources).
4. Recompute spec_stats with fill rate denominator = products in category.
Idempotent: running twice yields identical state.
"""
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sqlite3
import sys
import time

# Ensure project root is in sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.spec.taxonomy_loader import get_taxonomy, get_extractors, load_taxonomy
from src.spec.extractor import classify_category, extract_specs_for_category
from src.spec.migration import run_spec_migration
import db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def sync_spec_definitions(conn: sqlite3.Connection, taxonomy) -> None:
    """Syncs spec_definitions table from YAML taxonomy. Deletes removed attributes and logs count."""
    logger.info("Step 1: Syncing spec_definitions from YAML...")
    cur = conn.cursor()
    
    yaml_keys = set()
    upsert_count = 0
    
    for cat_id, cat in taxonomy.categories.items():
        for attr_key, attr in cat.attributes.items():
            yaml_keys.add((cat_id, attr_key))
            enum_vals_json = json.dumps(attr.values) if attr.values is not None else None
            
            cur.execute("""
                INSERT INTO spec_definitions (category, key, display_name, value_type, unit, enum_values)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(category, key) DO UPDATE SET
                    display_name = excluded.display_name,
                    value_type = excluded.value_type,
                    unit = excluded.unit,
                    enum_values = excluded.enum_values;
            """, (cat_id, attr_key, attr.display_name, attr.type, attr.unit, enum_vals_json))
            upsert_count += 1

    # Find and delete removed spec definitions
    cur.execute("SELECT id, category, key FROM spec_definitions;")
    existing = cur.fetchall()
    deleted_count = 0
    for row_id, cat_id, attr_key in existing:
        if (cat_id, attr_key) not in yaml_keys:
            cur.execute("DELETE FROM spec_definitions WHERE id = ?;", (row_id,))
            deleted_count += 1
            
    conn.commit()
    logger.info(f"spec_definitions sync complete: {upsert_count} active attributes upserted, {deleted_count} removed attributes deleted.")

def classify_catalog(conn: sqlite3.Connection, taxonomy) -> Dict[str, int]:
    """Classifies every product in products table and updates spec_category."""
    logger.info("Step 2: Classifying full catalog across taxonomy categories...")
    t0 = time.time()
    cur = conn.cursor()
    cur.execute("SELECT id, title FROM products;")
    products = cur.fetchall()
    total = len(products)
    
    updates = []
    category_counts: Dict[str, int] = {}
    null_count = 0

    for pid, title in products:
        cat_res = classify_category(title, taxonomy)
        if cat_res:
            cat_id = cat_res[0]
            updates.append((cat_id, pid))
            category_counts[cat_id] = category_counts.get(cat_id, 0) + 1
        else:
            updates.append((None, pid))
            null_count += 1

    # Batch update products
    batch_size = 5000
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        cur.executemany("UPDATE products SET spec_category = ? WHERE id = ?;", batch)
    conn.commit()

    elapsed = time.time() - t0
    logger.info(
        f"Classification complete in {elapsed:.2f}s for {total} products: "
        f"{category_counts}, NULL={null_count} ({null_count / max(total, 1):.1%})"
    )
    return category_counts

def extract_and_store_specs(conn: sqlite3.Connection, taxonomy, extractors) -> None:
    """Extracts specs for all products with a spec_category (DELETE-THEN-INSERT per product, preserving manual source)."""
    logger.info("Step 3: Extracting specs for categorized products...")
    t0 = time.time()
    cur = conn.cursor()

    # Clear previous automated warnings for clean idempotent run
    cur.execute("DELETE FROM spec_extraction_warnings;")

    # Fetch all categorized products
    cur.execute("SELECT id, title, spec_category FROM products WHERE spec_category IS NOT NULL;")
    items = cur.fetchall()
    total_items = len(items)
    logger.info(f"Processing spec extraction for {total_items} categorized products...")

    specs_to_insert = []
    warnings_to_insert = []
    pids_to_clean = []

    for pid, title, cat_id in items:
        pids_to_clean.append((pid,))
        specs, warnings = extract_specs_for_category(title, cat_id, taxonomy, extractors)
        
        for w in warnings:
            warnings_to_insert.append((pid, w.category, w.key, w.reason, w.raw))

        for attr_key, s in specs.items():
            specs_to_insert.append((
                pid, s.key, s.value, s.value_num, s.value_type, s.confidence, s.evidence, s.source
            ))

    # DELETE existing non-manual rows in batches
    batch_size = 5000
    for i in range(0, len(pids_to_clean), batch_size):
        batch = pids_to_clean[i:i + batch_size]
        # Preserve manual rows and promoted vendor specs; rerunning the curated
        # backfill must not wipe the extension layer.
        cur.executemany(
            "DELETE FROM product_specs WHERE product_id = ? AND source NOT IN ('manual', 'promoted');",
            batch,
        )

    # INSERT new specs
    for i in range(0, len(specs_to_insert), batch_size):
        batch = specs_to_insert[i:i + batch_size]
        cur.executemany("""
            INSERT OR REPLACE INTO product_specs (
                product_id, key, value, value_num, value_type, confidence, evidence, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, batch)

    # INSERT warnings
    for i in range(0, len(warnings_to_insert), batch_size):
        batch = warnings_to_insert[i:i + batch_size]
        cur.executemany("""
            INSERT INTO spec_extraction_warnings (
                product_id, category, key, reason, raw
            ) VALUES (?, ?, ?, ?, ?);
        """, batch)

    conn.commit()
    elapsed = time.time() - t0
    logger.info(
        f"Spec extraction complete in {elapsed:.2f}s: "
        f"{len(specs_to_insert)} specs stored, {len(warnings_to_insert)} extraction warnings recorded."
    )

def recompute_spec_stats(conn: sqlite3.Connection, taxonomy) -> None:
    """Recomputes spec_stats for each category and attribute. Denominator = products in category."""
    logger.info("Step 4: Recomputing spec_stats...")
    cur = conn.cursor()
    now_str = datetime.now(timezone.utc).isoformat()
    active_pairs = set()

    for cat_id, cat in taxonomy.categories.items():
        # Denominator = total products classified in this category
        cur.execute("SELECT COUNT(1) FROM products WHERE spec_category = ?;", (cat_id,))
        applicable_count = cur.fetchone()[0]

        for attr_key, attr in cat.attributes.items():
            active_pairs.add((cat_id, attr_key))

            # Filled count: distinct products having this spec in this category
            cur.execute("""
                SELECT COUNT(1) FROM product_specs ps
                JOIN products p ON ps.product_id = p.id
                WHERE p.spec_category = ? AND ps.key = ?;
            """, (cat_id, attr_key))
            filled_count = cur.fetchone()[0]

            # Top value counts
            cur.execute("""
                SELECT ps.value, COUNT(1) as cnt
                FROM product_specs ps
                JOIN products p ON ps.product_id = p.id
                WHERE p.spec_category = ? AND ps.key = ?
                GROUP BY ps.value
                ORDER BY cnt DESC
                LIMIT 50;
            """, (cat_id, attr_key))
            val_counts = {row[0]: row[1] for row in cur.fetchall()}

            # Min and Max for numeric values
            cur.execute("""
                SELECT MIN(ps.value_num), MAX(ps.value_num)
                FROM product_specs ps
                JOIN products p ON ps.product_id = p.id
                WHERE p.spec_category = ? AND ps.key = ? AND ps.value_num IS NOT NULL;
            """, (cat_id, attr_key))
            min_max_row = cur.fetchone()
            val_min = min_max_row[0] if min_max_row and min_max_row[0] is not None else None
            val_max = min_max_row[1] if min_max_row and min_max_row[1] is not None else None

            cur.execute("""
                INSERT INTO spec_stats (
                    category, key, applicable_count, filled_count, value_counts, value_min, value_max, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(category, key) DO UPDATE SET
                    applicable_count = excluded.applicable_count,
                    filled_count = excluded.filled_count,
                    value_counts = excluded.value_counts,
                    value_min = excluded.value_min,
                    value_max = excluded.value_max,
                    updated_at = excluded.updated_at;
            """, (
                cat_id, attr_key, applicable_count, filled_count,
                json.dumps(val_counts), val_min, val_max, now_str
            ))

    # Remove any old stats for deleted attributes
    cur.execute("SELECT category, key FROM spec_stats;")
    for cat_id, attr_key in cur.fetchall():
        if (cat_id, attr_key) not in active_pairs:
            cur.execute("DELETE FROM spec_stats WHERE category = ? AND key = ?;", (cat_id, attr_key))

    conn.commit()
    logger.info("spec_stats recomputation complete.")

def recompute_spec_stats_global(conn: sqlite3.Connection) -> None:
    """Populates spec_stats_global from product_specs directly without category join."""
    logger.info("Step 5: Populating spec_stats_global from product_specs...")
    cur = conn.cursor()
    now_str = datetime.now(timezone.utc).isoformat()

    # Ensure spec_stats_global exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spec_stats_global (
            key TEXT PRIMARY KEY,
            value_type TEXT,
            applicable_count INTEGER,
            value_counts TEXT,
            value_min REAL,
            value_max REAL,
            updated_at TEXT
        );
    """)

    # Group by key and value_type
    cur.execute("""
        SELECT key, value_type, COUNT(1) as cnt
        FROM product_specs
        GROUP BY key, value_type;
    """)
    key_rows = cur.fetchall()

    cur.execute("DELETE FROM spec_stats_global;")

    for key, value_type, count in key_rows:
        # Top 50 value counts
        cur.execute("""
            SELECT value, COUNT(1) as cnt
            FROM product_specs
            WHERE key = ?
            GROUP BY value
            ORDER BY cnt DESC
            LIMIT 50;
        """, (key,))
        val_counts = {row[0]: row[1] for row in cur.fetchall()}

        # Min and Max for numeric values
        cur.execute("""
            SELECT MIN(value_num), MAX(value_num)
            FROM product_specs
            WHERE key = ? AND value_num IS NOT NULL;
        """, (key,))
        min_max = cur.fetchone()
        val_min = min_max[0] if min_max and min_max[0] is not None else None
        val_max = min_max[1] if min_max and min_max[1] is not None else None

        cur.execute("""
            INSERT INTO spec_stats_global (
                key, value_type, applicable_count, value_counts, value_min, value_max, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
        """, (key, value_type, count, json.dumps(val_counts), val_min, val_max, now_str))

    conn.commit()
    logger.info(f"spec_stats_global population complete: {len(key_rows)} keys populated.")

def run_backfill() -> None:
    """Executes the full idempotent backfill process."""
    start_time = time.time()
    logger.info("Starting Taxonomy & Spec Backfill...")

    conn = db.get_db_connection()
    try:
        # Ensure migration is applied
        run_spec_migration(conn)

        # Load taxonomy & extractors (runs load-time unit tests)
        taxonomy, extractors = load_taxonomy(reload=True)

        # Step 1: Sync definitions
        sync_spec_definitions(conn, taxonomy)

        # Step 2: Classify catalog
        classify_catalog(conn, taxonomy)

        # Step 3: Extract specs
        extract_and_store_specs(conn, taxonomy, extractors)

        # Step 4: Recompute stats per category
        recompute_spec_stats(conn, taxonomy)

        # Step 5: Recompute global spec stats
        recompute_spec_stats_global(conn)

        elapsed = time.time() - start_time
        logger.info(f"Backfill finished successfully in {elapsed:.2f}s!")
    finally:
        conn.close()

if __name__ == "__main__":
    run_backfill()
