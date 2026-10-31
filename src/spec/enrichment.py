"""Transactional enrichment shared by ingestion and offline catalog repair.

Only this pipeline's derived rows are replaced. Reviewed/legacy promoted facts,
raw vendor attributes and the original catalog listings remain intact.
"""
import hashlib
import json
from datetime import datetime, timezone

from src.spec.description import extract_description_attributes
from src.spec.title_attributes import extract_title_attributes
from src.spec.taxonomy_loader import classify_product, get_taxonomy
from src.spec.vendor_specs import build_promoted_specs
from src.spec.extractor import extract_specs_for_category
from src.spec.vendor_index import _rows_for_attribute, _SOURCE_PRIORITY

VERSION = "spec-enrichment-1"


def ensure_enrichment_schema(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS spec_enrichment_state (product_id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, version TEXT NOT NULL, updated_at TEXT NOT NULL)")


def _attributes(conn, product_id):
    return conn.execute("SELECT key,label,value,value_num,unit,source,confidence FROM product_attributes WHERE product_id=? ORDER BY key,source", (product_id,)).fetchall()


def _digest(title, category, description, attrs):
    return hashlib.sha256(json.dumps([title,category,description,[list(a) for a in attrs]],sort_keys=True,default=str).encode()).hexdigest()


def enrich_listing(conn, product_id, *, description_html=None, force=False):
    """Refresh one listing's derived evidence without committing its transaction."""
    row = conn.execute("SELECT title,category,category_canonical FROM products WHERE id=?", (product_id,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown product {product_id}")
    title, raw_category, existing_category = row
    category = existing_category or classify_product(raw_category, title)[0]
    old_description = conn.execute("SELECT description FROM product_descriptions WHERE product_id=?", (product_id,)).fetchone()
    description = description_html if description_html is not None else (old_description[0] if old_description else None)
    attrs = _attributes(conn, product_id)
    fingerprint = _digest(title, category, description, attrs)
    previous = conn.execute("SELECT fingerprint,version FROM spec_enrichment_state WHERE product_id=?", (product_id,)).fetchone()
    if not force and previous and tuple(previous) == (fingerprint, VERSION):
        return {"changed":False,"keys":set(),"vendor_keys":set()}
    now = datetime.now(timezone.utc).isoformat()
    old_keys = {r[0] for r in conn.execute("SELECT key FROM product_specs WHERE product_id=?", (product_id,))}
    old_vendor_keys = {r[0] for r in conn.execute("SELECT key FROM product_vendor_specs WHERE product_id=?", (product_id,))}
    conn.execute("UPDATE products SET category_canonical=? WHERE id=?", (category, product_id))
    if description_html is not None:
        conn.execute("INSERT INTO product_descriptions(product_id,description,source,fetched_at) VALUES(?,?,'ingestion',?) ON CONFLICT(product_id) DO UPDATE SET description=excluded.description,source=excluded.source,fetched_at=excluded.fetched_at", (product_id, description_html, now))
    # Replace only the sources that can be regenerated from the supplied inputs.
    conn.execute("DELETE FROM product_attributes WHERE product_id=? AND source='title_heuristic'", (product_id,))
    derived = extract_title_attributes(title, category)
    if description_html is not None or force or not previous or previous[1] != VERSION:
        if description_html is not None:
            conn.execute("DELETE FROM product_attributes WHERE product_id=? AND source IN ('description_table','description_text')", (product_id,))
        derived += extract_description_attributes(description, max_attributes=None)
    conn.executemany("INSERT OR REPLACE INTO product_attributes(product_id,key,label,value,value_num,unit,source,confidence) VALUES(?,?,?,?,?,?,?,?)", [
        (product_id,a['key'],a.get('label',a['key']),a['value'],a.get('value_num'),a.get('unit'),a['source'],a.get('confidence',0.6))
        for a in derived])
    attrs = _attributes(conn, product_id)
    # Stronger evidence is inserted last. Unlike sorting by a vendor's source
    # spelling, this preserves tables over title heuristics on collisions.
    attrs = sorted(attrs, key=lambda a: (_SOURCE_PRIORITY.get(a[5],0), a[6] or 0))
    normalized, merged = {}, {}
    for key,label,value,number,unit,source,confidence in attrs:
        merged[key] = {'key':key,'value':value,'value_num':number,'unit':unit,'source':source,'confidence':confidence}
        for candidate in _rows_for_attribute(product_id,category,key,value,number,unit,source,confidence):
            normalized[candidate[1]] = candidate
    conn.execute("DELETE FROM product_vendor_specs WHERE product_id=? AND (source!='vendor' OR raw_key IN (SELECT key FROM product_attributes WHERE product_id=?))", (product_id,product_id))
    conn.executemany("INSERT OR REPLACE INTO product_vendor_specs(product_id,key,value,value_num,value_type,unit,raw_key,confidence,source) VALUES(?,?,?,?,?,?,?,?,?)", normalized.values())
    taxonomy = get_taxonomy()
    specs = build_promoted_specs(title,category,merged,taxonomy)
    rules, _ = extract_specs_for_category(title,category,taxonomy)
    by_key = {key:vars(spec) for key,spec in rules.items()}
    by_key.update({spec['key']:spec for spec in specs})
    conn.execute("DELETE FROM product_specs WHERE product_id=? AND source='enrichment'", (product_id,))
    for key, spec in by_key.items():
        # Existing independent/promoted evidence keeps precedence.
        conn.execute("INSERT OR IGNORE INTO product_specs(product_id,key,value,value_num,value_type,confidence,evidence,source) VALUES(?,?,?,?,?,?,?,'enrichment')",
                     (product_id,key,spec['value'],spec.get('value_num'),spec['value_type'],spec.get('confidence',0.6),spec.get('evidence','')))
        definition = taxonomy.categories[category].attributes.get(key)
        if definition:
            conn.execute("INSERT OR IGNORE INTO spec_definitions(category,key,display_name,value_type,unit,enum_values) VALUES(?,?,?,?,?,?)", (category,key,definition.display_name,definition.type,definition.unit,json.dumps(definition.values) if definition.values else None))
    if by_key:
        conn.execute("UPDATE products SET spec_category=? WHERE id=? AND spec_category IS NULL", (category, product_id))
    fingerprint = _digest(title,category,description,_attributes(conn,product_id))
    conn.execute("INSERT INTO spec_enrichment_state(product_id,fingerprint,version,updated_at) VALUES(?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET fingerprint=excluded.fingerprint,version=excluded.version,updated_at=excluded.updated_at", (product_id,fingerprint,VERSION,now))
    return {"changed":True,"keys":old_keys | set(by_key),"vendor_keys":old_vendor_keys | set(normalized)}


def refresh_statistics(conn, keys, vendor_keys):
    """Refresh affected keys only; no commits or full-catalog index rebuilds."""
    now = datetime.now(timezone.utc).isoformat()
    for key in vendor_keys:
        conn.execute("DELETE FROM vendor_spec_stats_global WHERE key=?", (key,))
        conn.execute("INSERT INTO vendor_spec_stats_global(key,value_type,unit,applicable_count,value_min,value_max,updated_at) SELECT key,CASE WHEN SUM(value_num IS NOT NULL)>0 THEN 'float' ELSE 'string' END,MAX(unit),COUNT(DISTINCT product_id),MIN(value_num),MAX(value_num),? FROM product_vendor_specs WHERE key=? GROUP BY key", (now,key))
        conn.execute("DELETE FROM vendor_spec_stats WHERE key=?", (key,))
        conn.execute("INSERT INTO vendor_spec_stats(category,key,applicable_count,filled_count,unit,value_min,value_max,updated_at) SELECT p.category_canonical,v.key,COUNT(DISTINCT p.id),COUNT(DISTINCT p.id),MAX(v.unit),MIN(v.value_num),MAX(v.value_num),? FROM product_vendor_specs v JOIN products p ON p.id=v.product_id WHERE v.key=? AND p.category_canonical IS NOT NULL GROUP BY p.category_canonical,v.key", (now,key))
    for key in keys:
        values = dict(conn.execute("SELECT value,COUNT(DISTINCT product_id) FROM product_specs WHERE key=? GROUP BY value", (key,)))
        conn.execute("DELETE FROM spec_stats_global WHERE key=?", (key,))
        conn.execute("INSERT INTO spec_stats_global(key,value_type,applicable_count,value_counts,value_min,value_max,updated_at) SELECT key,MAX(value_type),COUNT(DISTINCT product_id),?,MIN(value_num),MAX(value_num),? FROM product_specs WHERE key=? GROUP BY key", (json.dumps(values),now,key))
        conn.execute("DELETE FROM spec_stats WHERE key=?", (key,))
        for category, in conn.execute("SELECT DISTINCT category_canonical FROM products p JOIN product_specs s ON s.product_id=p.id WHERE s.key=? AND p.category_canonical IS NOT NULL", (key,)).fetchall():
            applicable = conn.execute("SELECT COUNT(*) FROM products WHERE category_canonical=?", (category,)).fetchone()[0]
            counts = dict(conn.execute("SELECT s.value,COUNT(DISTINCT s.product_id) FROM product_specs s JOIN products p ON p.id=s.product_id WHERE s.key=? AND p.category_canonical=? GROUP BY s.value", (key,category)))
            conn.execute("INSERT INTO spec_stats(category,key,applicable_count,filled_count,value_counts,value_min,value_max,updated_at) SELECT ?,?, ?,COUNT(DISTINCT s.product_id),?,MIN(s.value_num),MAX(s.value_num),? FROM product_specs s JOIN products p ON p.id=s.product_id WHERE s.key=? AND p.category_canonical=?", (category,key,applicable,json.dumps(counts),now,key,category))
