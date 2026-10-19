"""
Database migration runner for spec tables and products.spec_category.
Idempotent and safe to run multiple times.
"""
import logging
from pathlib import Path
import sqlite3

logger = logging.getLogger(__name__)

def run_spec_migration(conn: sqlite3.Connection) -> None:
    """Applies schema migration for spec definitions, product specs, spec stats, warnings, and products.spec_category."""
    base_dir = Path(__file__).resolve().parent.parent.parent
    sql_path = base_dir / "migrations" / "001_spec_tables.sql"

    with open(sql_path, "r", encoding="utf-8") as f:
        sql_script = f.read()

    with conn:
        conn.executescript(sql_script)

        # Check and add spec_category column to products table if missing
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(products);")
        columns = [row[1] for row in cur.fetchall()]
        if "spec_category" not in columns:
            logger.info("Adding 'spec_category' column to 'products' table...")
            conn.execute("ALTER TABLE products ADD COLUMN spec_category TEXT;")
        
        # Create index on products(spec_category)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_spec_category ON products(spec_category);")
        logger.info("Spec migration applied successfully.")
