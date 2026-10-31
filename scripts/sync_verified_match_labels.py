"""Export only human-verified matcher judgments from a local DB to the eval suite."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.evaluation.reporting import atomic_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data/components.db")
    parser.add_argument("--output", type=Path, default=ROOT / "evaluations/real_matching_labels.json")
    parser.add_argument("--minimum", type=int, default=1, help="Refuse to replace output below this many verified labels.")
    args = parser.parse_args()
    db_path, output = args.db.resolve(), args.output.resolve()
    if output == db_path or output.is_relative_to(db_path):
        parser.error("Output cannot overwrite or reside inside the catalog database.")
    conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT pair_id,title_a,title_b,label,confidence,reason,resolved_at,human_verified "
            "FROM llm_labels WHERE human_verified=1 ORDER BY pair_id"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < args.minimum:
        raise SystemExit(f"Refusing export: found {len(rows)} verified labels; minimum is {args.minimum}.")
    pairs = []
    for row in rows:
        pair = dict(row)
        expected = hashlib.sha1("::".join(sorted([pair["title_a"].lower().strip(), pair["title_b"].lower().strip()])).encode()).hexdigest()[:16]
        if pair["pair_id"] != expected or pair["label"] not in {"same", "different", "unsure"}:
            raise SystemExit(f"Invalid verified judgment in local DB: {pair['pair_id']}")
        pairs.append({"id": pair["pair_id"], "title_a": pair["title_a"], "title_b": pair["title_b"],
                      "label": pair["label"], "confidence": pair["confidence"], "reason": pair["reason"],
                      "adjudicated_at": pair["resolved_at"], "human_verified": True})
    artifact = {
        "version": "1.0.0",
        "provenance": "Exported only from local llm_labels rows with human_verified=1. Active-review/LLM-only labels and matcher decisions are excluded. This selected production stress slice is not a random held-out sample.",
        "pairs": pairs,
    }
    atomic_text(output, json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
    print(f"Exported {len(pairs)} human-verified matcher judgments to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
