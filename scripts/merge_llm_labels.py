#!/usr/bin/env python3
"""
Ingests resolver classifications from data/resolver_output.jsonl into the llm_labels SQLite table.
Marks rows matching the 40-pair human-labeled seed with human_verified = 1.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "components.db"
RESOLVER_OUTPUT = DATA_DIR / "resolver_output.jsonl"
SEED_FILE = DATA_DIR / "judge_seed.jsonl"


def compute_pair_id(title_a: str, title_b: str) -> str:
    norm = sorted([title_a.lower().strip(), title_b.lower().strip()])
    return hashlib.sha1("::".join(norm).encode("utf-8")).hexdigest()[:16]


def init_llm_labels_table(conn: sqlite3.Connection):
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_labels (
                pair_id TEXT PRIMARY KEY,
                title_a TEXT,
                title_b TEXT,
                label TEXT NOT NULL,
                confidence TEXT,
                reason TEXT,
                resolved_at TEXT,
                human_verified INTEGER DEFAULT 0
            );
        """)


def load_seed_title_pairs(seed_path: Path) -> set:
    seed_pairs = set()
    if seed_path.exists():
        with open(seed_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        d = json.loads(line)
                        t_a = d.get("title_a", "").lower().strip()
                        t_b = d.get("title_b", "").lower().strip()
                        if t_a and t_b:
                            seed_pairs.add(tuple(sorted([t_a, t_b])))
                    except Exception:
                        continue
    return seed_pairs


def merge_llm_labels(resolver_output_path: Path = RESOLVER_OUTPUT, db_path: Path = DB_PATH):
    conn = sqlite3.connect(str(db_path))
    init_llm_labels_table(conn)

    seed_pairs = load_seed_title_pairs(SEED_FILE)
    print(f"Loaded {len(seed_pairs)} seed pairs for human_verified matching.")

    rows_to_upsert = []
    if resolver_output_path.exists():
        with open(resolver_output_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                t_a = item.get("title_a", "")
                t_b = item.get("title_b", "")
                pid = item.get("pair_id")
                if not pid or pid.startswith("gz_"):
                    pid = compute_pair_id(t_a, t_b)

                # Check if in seed
                title_key = tuple(sorted([t_a.lower().strip(), t_b.lower().strip()]))
                is_human = 1 if title_key in seed_pairs else 0

                resolved_at = item.get("resolved_at") or datetime.now(timezone.utc).isoformat()
                rows_to_upsert.append((
                    pid,
                    t_a,
                    t_b,
                    item.get("label", "unsure"),
                    item.get("confidence", "medium"),
                    item.get("reason", ""),
                    resolved_at,
                    is_human
                ))

    # Also, if data/judge_output.jsonl exists, ensure the 40 seed validations are present with human_verified=1
    judge_output_file = DATA_DIR / "judge_output.jsonl"
    if judge_output_file.exists() and SEED_FILE.exists():
        seed_map = {}
        with open(SEED_FILE, "r", encoding="utf-8") as sf:
            for line in sf:
                if line.strip():
                    d = json.loads(line)
                    seed_map[d["pair_id"]] = d

        with open(judge_output_file, "r", encoding="utf-8") as jf:
            for line in jf:
                if line.strip():
                    item = json.loads(line)
                    pid_legacy = item.get("pair_id")
                    if pid_legacy in seed_map:
                        seed_item = seed_map[pid_legacy]
                        t_a = seed_item.get("title_a", "")
                        t_b = seed_item.get("title_b", "")
                        pid_sha1 = compute_pair_id(t_a, t_b)
                        # Check if already added
                        if not any(r[0] == pid_sha1 for r in rows_to_upsert):
                            rows_to_upsert.append((
                                pid_sha1,
                                t_a,
                                t_b,
                                item.get("label", "unsure"),
                                item.get("confidence", "high"),
                                item.get("reason", ""),
                                seed_item.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                                1  # human_verified = 1
                            ))

    if rows_to_upsert:
        with conn:
            conn.executemany("""
                INSERT INTO llm_labels (
                    pair_id, title_a, title_b, label, confidence, reason, resolved_at, human_verified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair_id) DO UPDATE SET
                    label=excluded.label,
                    confidence=excluded.confidence,
                    reason=excluded.reason,
                    resolved_at=excluded.resolved_at,
                    human_verified=MAX(llm_labels.human_verified, excluded.human_verified);
            """, rows_to_upsert)

    # Compute report
    c = conn.cursor()
    c.execute("SELECT label, confidence, human_verified FROM llm_labels")
    all_labels = c.fetchall()
    conn.close()

    total_count = len(all_labels)
    label_dist = Counter(r[0] for r in all_labels)
    unsure_count = label_dist.get("unsure", 0)
    low_conf_count = sum(1 for r in all_labels if r[1] == "low")
    human_verified_count = sum(1 for r in all_labels if r[2] == 1)

    print("=" * 60)
    print("LLM LABELS INGESTION REPORT")
    print("=" * 60)
    print(f"Total rows in llm_labels: {total_count}")
    print(f"Label distribution:       {dict(label_dist)}")
    print(f"Unsure count (no-merge):  {unsure_count}")
    print(f"Low-confidence count:     {low_conf_count}")
    print(f"Human verified count:     {human_verified_count}")
    print("=" * 60)


if __name__ == "__main__":
    merge_llm_labels()
