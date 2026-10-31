"""Prepare blinded, human-judged search and hard-negative candidates from a local catalog.

The catalog is opened in SQLite read-only mode. Queue candidates and automatic
matcher decisions are never treated as relevance or match labels.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.evaluation.reporting import atomic_text

OUTPUT = ROOT / "reports/backend-quality-review"


def connect_read_only(path: Path):
    resolved = path.resolve()
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def stable_id(kind, *values):
    value = "\x1f".join(str(item or "").strip().casefold() for item in values)
    return f"{kind}_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def search_candidates(conn, query, limit=20):
    tokens = list(dict.fromkeys(re.findall(r"[\w]+", query, flags=re.UNICODE)))
    tokens = [token for token in tokens if len(token) >= 2][:12]
    if not tokens:
        return []
    expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    try:
        rows = conn.execute(
            "SELECT p.id,p.vendor,p.title,p.mpn,p.category_canonical,p.price,p.available,p.product_url "
            "FROM products_fts JOIN products p ON p.id=products_fts.rowid "
            "WHERE products_fts MATCH ? ORDER BY bm25(products_fts) LIMIT ?",
            (expression, limit),
        )
        return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


def build_search_queue(conn):
    rows = conn.execute("SELECT query,count,last_seen FROM search_queries WHERE trim(query)<>'' ORDER BY query COLLATE NOCASE")
    queue = []
    for query, count, last_seen in rows:
        candidates = search_candidates(conn, query)
        queue.append({
            "id": stable_id("query", query), "query": query, "observed_count": int(count),
            "last_seen": last_seen, "source": "local_search_queries_log",
            "judgment_status": "pending", "judgment_scope": "candidate_pool_only_not_exhaustive",
            "candidate_listings": [
                {"listing_id": row["id"], "vendor": row["vendor"], "title": row["title"],
                 "mpn": row["mpn"], "category": row["category_canonical"], "price": row["price"],
                 "available": bool(row["available"]), "product_url": row["product_url"],
                 "relevance_grade": None, "rationale": None}
                for row in candidates
            ],
            "unjudged_relevant_listing_ids": None,
            "reviewer": None, "reviewed_at": None, "review_notes": None,
        })
    return queue


def build_hard_negative_candidates(conn, limit):
    # These are model-merged pairs with a numeric-variant warning. Selection is
    # solely for reviewer efficiency; the output deliberately hides model labels,
    # scores, reasons, and feature flags to reduce anchoring.
    rows = conn.execute(
        "SELECT id,vendor_a,title_a,mpn_a,price_a,vendor_b,title_b,mpn_b,price_b "
        "FROM matcher_gray_zone WHERE decision='merged' "
        "AND (flags LIKE '%NUMERIC_DIVERGENCE%' OR flags LIKE '%ASYMMETRIC_MPN_MERGE%') "
        "AND trim(title_a)<>'' AND trim(title_b)<>''"
    )
    unique = {}
    for row in rows:
        db_id, vendor_a, title_a, mpn_a, price_a, vendor_b, title_b, mpn_b, price_b = row
        pair_key = tuple(sorted((title_a.strip().casefold(), title_b.strip().casefold())))
        if not pair_key[0] or not pair_key[1] or pair_key[0] == pair_key[1]:
            continue
        pair_id = stable_id("hard_negative_candidate", *pair_key)
        record = {
            "id": pair_id, "title_a": title_a, "title_b": title_b,
            "vendor_a": vendor_a, "vendor_b": vendor_b,
            "mpn_a": mpn_a, "mpn_b": mpn_b,
            "price_a": price_a, "price_b": price_b,
            "candidate_type": "hard_negative_candidate_unconfirmed",
            "source": "matcher_gray_zone_numeric_variant_pool",
            "source_record_id": db_id,
            "judgment": None, "rationale": None, "reviewer": None, "reviewed_at": None,
        }
        unique.setdefault(pair_id, record)
    candidates = sorted(unique.values(), key=lambda row: row["id"])
    return candidates[:limit], len(unique)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/components.db", help="Local catalog; opened read-only.")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--hard-negatives", type=int, default=500, help="Maximum hard-negative candidates to queue.")
    args = parser.parse_args()
    if args.hard_negatives < 0:
        parser.error("--hard-negatives must be nonnegative")
    output = args.output.resolve()
    if output == args.db.resolve() or args.db.resolve().is_relative_to(output):
        parser.error("Review output cannot overwrite or contain the catalog database.")

    conn = connect_read_only(args.db)
    try:
        searches = build_search_queue(conn)
        hard_negatives, hard_pool_size = build_hard_negative_candidates(conn, args.hard_negatives)
        click_count = conn.execute("SELECT COUNT(*) FROM ui_clicks").fetchone()[0]
        verified_count = conn.execute("SELECT COUNT(*) FROM llm_labels WHERE human_verified=1").fetchone()[0]
        matcher_gray_count = conn.execute("SELECT COUNT(*) FROM matcher_gray_zone").fetchone()[0]
    finally:
        conn.close()

    output.mkdir(parents=True, exist_ok=True)
    atomic_text(output / "search_queries.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in searches))
    atomic_text(output / "hard_negative_candidates.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in hard_negatives))
    metadata = {
        "format_version": "1.0.0", "catalog_path": str(args.db.resolve()),
        "search_queries": len(searches), "search_click_events_available": click_count,
        "matcher_gray_zone_rows": matcher_gray_count,
        "human_verified_match_labels_available": verified_count,
        "hard_negative_candidate_pool": hard_pool_size,
        "hard_negative_candidates_queued": len(hard_negatives),
        "created_from_read_only_catalog": True,
        "warning": "Every queued relevance/match judgment is null. Model predictions and click counts are not ground truth.",
    }
    atomic_text(output / "manifest.json", json.dumps(metadata, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(searches)} observed queries and {len(hard_negatives)} of {hard_pool_size} hard-negative candidates to {output}")
    print(f"Only {click_count} click event(s) are available; all search relevance remains unjudged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
