"""
Comparison script between LLM Resolver and LLM Judge decisions.
Requirements:
- Join llm_resolutions and judge_labels on pair_id
- Emit agreement rate
- Emit data/human_review_queue.jsonl for disagreements
"""
import json
import sqlite3
import sys
from pathlib import Path
from typing import Dict, Any

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))


def compare_resolver_and_judge(
    db_path: Path = Path("data/components.db"),
    judge_labels_path: Path = Path("data/judge_labels.jsonl"),
    review_queue_path: Path = Path("data/human_review_queue.jsonl")
):
    # 1. Load Resolver resolutions from SQLite
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='llm_resolutions'")
    if not c.fetchone():
        print("Table 'llm_resolutions' does not exist yet. Run resolver before comparison.")
        conn.close()
        return

    c.execute("SELECT pair_id, label, reason, confidence FROM llm_resolutions")
    resolver_rows = {row["pair_id"]: dict(row) for row in c.fetchall()}
    conn.close()

    print(f"Loaded {len(resolver_rows)} resolver decisions from database.")

    # 2. Load Judge labels from file or table
    judge_rows: Dict[str, Dict[str, Any]] = {}
    if judge_labels_path.exists():
        with open(judge_labels_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    item = json.loads(line)
                    judge_rows[item["pair_id"]] = item
    print(f"Loaded {len(judge_rows)} judge decisions from {judge_labels_path}.")

    # 3. Join on pair_id
    common_ids = set(resolver_rows.keys()) & set(judge_rows.keys())
    if not common_ids:
        print("No overlapping pair_ids found between resolver and judge.")
        return

    agree_count = 0
    disagreements = []

    for pid in sorted(list(common_ids)):
        res = resolver_rows[pid]
        jdg = judge_rows[pid]

        res_label = res.get("label", "").lower()
        jdg_label = jdg.get("label", "").lower()

        if res_label == jdg_label:
            agree_count += 1
        else:
            disagreements.append({
                "pair_id": pid,
                "resolver_label": res_label,
                "resolver_reason": res.get("reason"),
                "judge_label": jdg_label,
                "judge_reason": jdg.get("reason"),
                "deciding_spec": jdg.get("deciding_spec")
            })

    total = len(common_ids)
    agreement_pct = (agree_count / total) * 100.0
    print(f"\n==================================================")
    print(f"RESOLVER VS JUDGE AGREEMENT REPORT")
    print(f"==================================================")
    print(f"Joined Pairs:   {total}")
    print(f"Agreements:     {agree_count}")
    print(f"Disagreements:  {len(disagreements)}")
    print(f"Agreement Rate: {agreement_pct:.2f}%")

    # 4. Emit human review queue
    review_queue_path.parent.mkdir(parents=True, exist_ok=True)
    with open(review_queue_path, "w", encoding="utf-8") as f:
        for item in disagreements:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    print(f"Emitted {len(disagreements)} disagreement items to {review_queue_path} for human inspection.\n")

if __name__ == "__main__":
    compare_resolver_and_judge()
