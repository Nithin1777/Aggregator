import sqlite3
from pathlib import Path

db_path = Path('data/components.db')
conn = sqlite3.connect(str(db_path))
c = conn.cursor()

# Get existing columns of products table
c.execute("PRAGMA table_info(products)")
columns = [row[1] for row in c.fetchall()]
print("Current products columns:", columns)

if 'is_canonical' not in columns:
    print("Adding is_canonical column...")
    c.execute("ALTER TABLE products ADD COLUMN is_canonical INTEGER NOT NULL DEFAULT 0")

if 'canonical_for' not in columns:
    print("Adding canonical_for column...")
    c.execute("ALTER TABLE products ADD COLUMN canonical_for TEXT")

if 'query_ctr' not in columns:
    print("Adding query_ctr column...")
    c.execute("ALTER TABLE products ADD COLUMN query_ctr REAL NULL")

# Indexes for fast canonical lookup
c.execute("CREATE INDEX IF NOT EXISTS idx_products_canonical ON products(is_canonical, canonical_for)")

# Task 2.2: variant_families table
c.execute("""
    CREATE TABLE IF NOT EXISTS variant_families (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_mpn TEXT NOT NULL,
        family_label TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
""")
c.execute("CREATE INDEX IF NOT EXISTS idx_variant_families_mpn ON variant_families(canonical_mpn)")

conn.commit()
conn.close()
print("Migration completed successfully.")
