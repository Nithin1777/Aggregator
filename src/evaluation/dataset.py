"""Load judgments and seed a closed catalog from explicit facts, not extractors."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[2]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def load_dataset(directory: Path = ROOT / "evaluations") -> dict:
    files = {name: json.loads((directory / name).read_text(encoding="utf-8"))
             for name in ("catalog.json", "search_cases.json", "matching_scenarios.json", "real_matching_labels.json")}
    legacy = json.loads((ROOT / "tests/data/matching_golden_set.json").read_text(encoding="utf-8"))
    catalog, searches, matching = (files[name] for name in ("catalog.json", "search_cases.json", "matching_scenarios.json"))
    reviewed_matching = files["real_matching_labels.json"]
    products = catalog["products"]
    keys = [p["key"] for p in catalog_products(catalog)]
    if len(keys) != len(set(keys)) or not products or any(not isinstance(key, str) or not key.strip() for key in keys):
        raise ValueError("Catalog keys must be unique and nonempty.")
    for section in (searches["queries"], searches["contracts"], searches["pagination"],
                    legacy, matching["pairs"], matching["groups"]):
        if not section:
            raise ValueError("Every evaluation section must contain cases.")
    entities = {p["entity"] for p in catalog_products(catalog)}
    ids = []
    for case in searches["queries"]:
        ids.append(case["id"])
        surfaces = case.get("surfaces", ["http", "mcp"])
        if not surfaces or len(surfaces) != len(set(surfaces)) or not set(surfaces) <= {"http", "mcp"}:
            raise ValueError(f"Invalid query surfaces: {case['id']}")
        if not set(case.get("allowed_listings", [])) <= set(keys):
            raise ValueError(f"Unknown allowed listing: {case['id']}")
        if not case.get("rationale") or not set(case["relevance"]) <= entities:
            raise ValueError(f"Missing rationale or unknown relevance entity: {case['id']}")
        if any(type(grade) is not int or not 0 <= grade <= 3 for grade in case["relevance"].values()):
            raise ValueError(f"Relevance grades must be integers 0..3: {case['id']}")
    for case in searches["contracts"] + searches["pagination"]:
        ids.append(case["id"])
        if case.get("expected_template"):
            template = catalog[case["expected_template"]]
            case["expected_entities"] = [f"{template['prefix']}_{i:03}" for i in range(template["count"])]
        if not set(case.get("expected_entities", [])) <= entities:
            raise ValueError(f"Unknown expected entity: {case['id']}")
    if len(ids) != len(set(ids)):
        raise ValueError("Search/contract/pagination IDs must be unique.")
    pair_ids = [p["id"] for p in legacy + matching["pairs"]]
    if len(pair_ids) != len(set(pair_ids)):
        raise ValueError("Matching pair IDs must be unique.")
    for pair in legacy + matching["pairs"]:
        if type(pair["should_match"]) is not bool or not pair["title_a"] or not pair["title_b"]:
            raise ValueError(f"Invalid matching judgment: {pair['id']}")
    reviewed_ids = []
    if not reviewed_matching["pairs"]:
        raise ValueError("The human-verified matcher stress set must not be empty.")
    for pair in reviewed_matching["pairs"]:
        reviewed_ids.append(pair["id"])
        if pair.get("label") not in {"same", "different", "unsure"} or pair.get("human_verified") is not True:
            raise ValueError(f"Reviewed real matcher labels must be human verified: {pair.get('id')}")
        if not pair.get("title_a") or not pair.get("title_b") or not pair.get("reason"):
            raise ValueError(f"Reviewed real matcher labels need titles and rationale: {pair.get('id')}")
        expected_hash = hashlib.sha1("::".join(sorted([pair["title_a"].lower().strip(), pair["title_b"].lower().strip()])).encode()).hexdigest()[:16]
        if pair["id"] != expected_hash:
            raise ValueError(f"Reviewed matcher label identity does not match title pair: {pair['id']}")
    if len(reviewed_ids) != len(set(reviewed_ids)) or set(reviewed_ids) & set(pair_ids):
        raise ValueError("Reviewed matcher label IDs must be unique and independent of existing fixture labels.")
    for group in matching["groups"]:
        if len(group["members"]) < 2 or len(group["members"]) != len(set(group["members"])) or not set(group["members"]) <= set(keys):
            raise ValueError(f"Invalid grouping members: {group['id']}")
    group_ids = [group["id"] for group in matching["groups"]]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("Grouping IDs must be unique.")
    if any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids + pair_ids + group_ids):
        raise ValueError("Case IDs must be nonempty strings.")
    return {"catalog": catalog, "searches": searches, "matching": matching, "legacy_pairs": legacy,
            "reviewed_matching_pairs": reviewed_matching["pairs"],
            "sha256": digest({**files, "legacy_pairs": legacy}), "catalog_sha256": digest(catalog)}


def catalog_products(catalog):
    products = list(catalog["products"])
    pagination = catalog["pagination_support"]
    for index in range(pagination["count"]):
        key = f"{pagination['prefix']}_{index:03}"
        products.append({"key": key, "entity": key, "vendor": "robu",
                         "title": f"Pagination sensor EVAL{index:04}", "mpn": f"EVAL{index:04}",
                         "category": pagination["category"], "price": 10 + index, "available": True})
    support = catalog["metadata_support"]
    for index in range(support["count"]):
        products.append({"key": f"metadata_{index:03}", "entity": f"metadata_{index:03}",
                         "vendor": "robu", "title": f"Metadata calibration coupon {index:03}",
                         "category": support["category"], "price": 1, "available": True,
                         "specs": support["specs"]})
    return products


def seed_catalog(path: Path, catalog) -> dict[int, dict]:
    # db is imported only after the runner has redirected settings.db_path.
    import db
    from config import settings
    from src.spec.vendor_index import recompute_vendor_stats

    if settings.db_path.resolve() != path.resolve():
        raise ValueError("Refusing to seed a database other than the configured evaluation path.")
    db.init_db()
    conn = db.get_db_connection()
    products = catalog_products(catalog)
    definitions = {}
    try:
        with conn:
            for category, specs in catalog["definitions"].items():
                for key, (kind, unit) in specs.items():
                    definitions[key] = (kind, unit)
                    conn.execute("INSERT INTO spec_definitions (category,key,display_name,value_type,unit) VALUES (?,?,?,?,?)",
                                 (category, key, key, kind, unit))
            for pid, product in enumerate(products, 1):
                conn.execute(
                    "INSERT INTO products (id,vendor,vendor_name,external_id,title,normalized_title,mpn,sku,price,available,product_url,category,category_canonical,spec_category,raw_json,price_stock_source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, product["vendor"], product["vendor"], product["key"], product["title"],
                     product["title"].lower(), product.get("mpn"), product["key"], product["price"],
                     int(product["available"]), f"https://fixture.invalid/{product['key']}", product["category"],
                     product["category"], product["category"], "{}", "cache"),
                )
                for key, value in product.get("specs", {}).items():
                    kind, _ = definitions[key]
                    number = value if isinstance(value, (int, float)) else None
                    conn.execute("INSERT INTO product_specs (product_id,key,value,value_num,value_type,confidence,evidence,source) VALUES (?,?,?,?,?,1,?,'fixture_fact')",
                                 (pid, key, str(value), number, kind, "Explicit versioned fixture fact"))
                for key, value in product.get("vendor_specs", {}).items():
                    conn.execute("INSERT INTO product_vendor_specs (product_id,key,value,value_num,value_type,unit,raw_key,confidence,source) VALUES (?,?,?,?,?,?,?,1,'description_table')",
                                 (pid, key, str(value), value, "float", "A" if key.endswith("_current_a") else None, key))
            for key, (kind, _) in definitions.items():
                values = conn.execute("SELECT value,value_num FROM product_specs WHERE key=?", (key,)).fetchall()
                numbers = [v[1] for v in values if v[1] is not None]
                conn.execute("INSERT INTO spec_stats_global (key,value_type,applicable_count,value_counts,value_min,value_max,updated_at) VALUES (?,?,?,?,?,?,?)",
                             (key, kind, len(values), json.dumps(dict(Counter(v[0] for v in values))),
                              min(numbers) if numbers else None, max(numbers) if numbers else None, "2026-01-01T00:00:00+00:00"))
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS products_fts_vocab USING fts5vocab('products_fts','row')")
        recompute_vendor_stats(conn)
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return {pid: product for pid, product in enumerate(products, 1)}


def restore_catalog(source: Path, target: Path):
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        try:
            src.close()
        finally:
            dst.close()
