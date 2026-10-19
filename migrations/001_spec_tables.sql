-- Migration: Taxonomy-Driven Spec Tables and Indexes

CREATE TABLE IF NOT EXISTS spec_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    value_type TEXT NOT NULL,
    unit TEXT,
    enum_values TEXT,
    UNIQUE(category, key)
);

CREATE TABLE IF NOT EXISTS product_specs (
    product_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    value_num REAL,
    value_type TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    evidence TEXT,
    source TEXT NOT NULL DEFAULT 'rule',
    PRIMARY KEY(product_id, key)
);

CREATE TABLE IF NOT EXISTS spec_stats (
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    applicable_count INTEGER NOT NULL DEFAULT 0,
    filled_count INTEGER NOT NULL DEFAULT 0,
    value_counts TEXT,
    value_min REAL,
    value_max REAL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(category, key)
);

CREATE TABLE IF NOT EXISTS spec_extraction_warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    category TEXT NOT NULL,
    key TEXT NOT NULL,
    reason TEXT NOT NULL,
    raw TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_product_specs_key_value ON product_specs(key, value);
CREATE INDEX IF NOT EXISTS idx_product_specs_key_value_num ON product_specs(key, value_num);
