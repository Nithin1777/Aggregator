#!/usr/bin/env python3
"""
Candidate pair extractor and bucketing pipeline.
Extracts candidate pairs across vendors, executes bucket_pair,
persists confident decisions to matcher_decisions table,
and writes gray pairs to data/gray_pairs.jsonl.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from normalizer import extract_hardware_specs
from src.llm.bucketing import bucket_pair

DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "components.db"
SEED_FILE = DATA_DIR / "judge_seed.jsonl"
GRAY_PAIRS_FILE = DATA_DIR / "gray_pairs.jsonl"

HIGH_FREQ_TOKENS = [
    "esp32", "esp8266", "stm32", "rp2040", "arduino", "raspberry pi", "pico", "nodemcu",
    "dht11", "dht22", "bme280", "bmp280", "mpu6050", "ina219", "max6675", "hc-sr04",
    "ds18b20", "vl53l0x", "max30102", "adxl345", "tcs3200", "mq-2", "mq-135",
    "lm2596", "xl4015", "ams1117", "tp4056", "mt3608", "mp1584", "lm7805", "tp5100",
    "l298n", "a4988", "drv8825", "l293d", "uln2003", "tb6600", "servo", "relay",
    "ch340", "cp2102", "ft232", "max485", "mcp2515", "nrf24l01", "sx1278",
    "oled", "1602", "2004", "tft", "bms", "sg90", "mg996r", "soil moisture", "rain drop"
]


def compute_pair_id(title_a: str, title_b: str) -> str:
    """Canonical content-addressed pair ID."""
    norm = sorted([title_a.lower().strip(), title_b.lower().strip()])
    return hashlib.sha1("::".join(norm).encode("utf-8")).hexdigest()[:16]


def get_excluded_title_pairs(seed_file: Path) -> set:
    """Loads title pairs from seed file to ensure disjoint extraction."""
    excluded = set()
    if seed_file.exists():
        with open(seed_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    t_a = d.get("title_a", "").lower().strip()
                    t_b = d.get("title_b", "").lower().strip()
                    if t_a and t_b:
                        excluded.add(tuple(sorted([t_a, t_b])))
                except Exception:
                    continue
    return excluded


def get_existing_llm_labels(conn: sqlite3.Connection) -> set:
    """Returns pair_ids already present in llm_labels table."""
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='llm_labels'")
    if not c.fetchone():
        return set()
    c.execute("SELECT pair_id FROM llm_labels")
    return {row[0] for row in c.fetchall()}


def init_db(conn: sqlite3.Connection):
    """Initializes matcher_decisions table."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS matcher_decisions (
                pair_id TEXT PRIMARY KEY,
                decision TEXT NOT NULL,
                score REAL,
                reason TEXT,
                created_at TEXT
            );
        """)


def extract_and_bucket_pairs():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    init_db(conn)

    excluded_titles = get_excluded_title_pairs(SEED_FILE)
    existing_labels = get_existing_llm_labels(conn)
    print(f"Loaded {len(excluded_titles)} excluded seed title pairs.")
    print(f"Loaded {len(existing_labels)} existing llm_labels pair_ids.")

    cur = conn.cursor()

    # 1. Candidate Collection
    # Candidates come from:
    # A) High-frequency tokens query across different vendors
    # B) Existing cross-vendor comparisons in matcher_gray_zone
    candidate_map = {}  # pair_id -> (title_a, price_a, mpn_a, title_b, price_b, mpn_b)

    print("Collecting candidate pairs from products table by high-frequency tokens...")
    for tok in HIGH_FREQ_TOKENS:
        cur.execute("""
            SELECT id, vendor, title, price, mpn 
            FROM products 
            WHERE title LIKE ? AND price > 10.0 
            LIMIT 40
        """, (f"%{tok}%",))
        rows = cur.fetchall()
        by_vendor = {}
        for r in rows:
            by_vendor.setdefault(r["vendor"], []).append(r)
        vendors = list(by_vendor.keys())
        for i in range(len(vendors)):
            for j in range(i + 1, len(vendors)):
                v1, v2 = vendors[i], vendors[j]
                for r1 in by_vendor[v1]:
                    for r2 in by_vendor[v2]:
                        t_a = r1["title"].strip()
                        t_b = r2["title"].strip()
                        pair_tuple = tuple(sorted([t_a.lower(), t_b.lower()]))
                        if pair_tuple in excluded_titles:
                            continue
                        pid = compute_pair_id(t_a, t_b)
                        if pid in existing_labels:
                            continue
                        if pid not in candidate_map:
                            candidate_map[pid] = (t_a, r1["price"] or 0.0, r1["mpn"], t_b, r2["price"] or 0.0, r2["mpn"])

    print(f"Candidates after token search: {len(candidate_map)}")

    # Also incorporate cross-vendor pairs from matcher_gray_zone
    cur.execute("""
        SELECT DISTINCT vendor_a, title_a, price_a, mpn_a, vendor_b, title_b, price_b, mpn_b 
        FROM matcher_gray_zone 
        WHERE vendor_a != vendor_b AND score >= 0.40
    """)
    for r in cur.fetchall():
        t_a = r["title_a"].strip()
        t_b = r["title_b"].strip()
        pair_tuple = tuple(sorted([t_a.lower(), t_b.lower()]))
        if pair_tuple in excluded_titles:
            continue
        pid = compute_pair_id(t_a, t_b)
        if pid in existing_labels:
            continue
        if pid not in candidate_map:
            candidate_map[pid] = (t_a, r["price_a"] or 0.0, r["mpn_a"], t_b, r["price_b"] or 0.0, r["mpn_b"])

    total_candidates = len(candidate_map)
    print(f"Total candidate pairs to process: {total_candidates}")

    # 2. Run bucket_pair on each candidate
    confident_merge_count = 0
    confident_reject_count = 0
    gray_count = 0

    now_ts = datetime.now(timezone.utc).isoformat()
    decisions_to_insert = []
    gray_records = []

    for pid, (t_a, p_a, m_a, t_b, p_b, m_b) in candidate_map.items():
        bucket, merged, score, reason = bucket_pair(t_a, t_b, p_a, p_b, m_a, m_b)

        if bucket == "confident_merge":
            confident_merge_count += 1
            decisions_to_insert.append((pid, "confident_merge", score, reason, now_ts))
        elif bucket == "confident_reject":
            confident_reject_count += 1
            decisions_to_insert.append((pid, "confident_reject", score, reason, now_ts))
        else:
            gray_count += 1
            specs_a = extract_hardware_specs(t_a)
            specs_b = extract_hardware_specs(t_b)
            gray_records.append({
                "pair_id": pid,
                "title_a": t_a,
                "specs_a": specs_a,
                "title_b": t_b,
                "specs_b": specs_b,
                "mpn_a": m_a,
                "mpn_b": m_b
            })

    # 3. Write confident decisions to SQLite
    if decisions_to_insert:
        with conn:
            conn.executemany("""
                INSERT OR REPLACE INTO matcher_decisions (pair_id, decision, score, reason, created_at)
                VALUES (?, ?, ?, ?, ?);
            """, decisions_to_insert)

    conn.close()

    # 4. Write gray pairs to JSONL
    GRAY_PAIRS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(GRAY_PAIRS_FILE, "w", encoding="utf-8") as f:
        for rec in gray_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 5. Output Report
    pct_merge = (confident_merge_count / total_candidates * 100.0) if total_candidates > 0 else 0.0
    pct_reject = (confident_reject_count / total_candidates * 100.0) if total_candidates > 0 else 0.0
    pct_gray = (gray_count / total_candidates * 100.0) if total_candidates > 0 else 0.0

    print("=" * 60)
    print("EXTRACTION AND BUCKETING REPORT")
    print("=" * 60)
    print(f"Total candidate pairs:  {total_candidates}")
    print(f"Confident merges:       {confident_merge_count} ({pct_merge:.2f}%)")
    print(f"Confident rejects:      {confident_reject_count} ({pct_reject:.2f}%)")
    print(f"Gray pairs:             {gray_count} ({pct_gray:.2f}%)")
    print(f"Written to:             {GRAY_PAIRS_FILE} ({len(gray_records)} rows)")
    print("=" * 60)

    if gray_count > 20000:
        print(f"WARNING: Gray count {gray_count} exceeds 20,000 threshold! STOPPING for cost review.")
        sys.exit(2)


if __name__ == "__main__":
    extract_and_bucket_pairs()
