"""Add completed hard-negative review rows to the versioned matcher benchmark.

This only changes the review/benchmark file. It never writes judgments to the
live matcher database, avoiding label leakage into production behavior.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.evaluation.reporting import atomic_text


def pair_identity(title_a, title_b):
    normalized = sorted([title_a.lower().strip(), title_b.lower().strip()])
    return hashlib.sha1("::".join(normalized).encode("utf-8")).hexdigest()[:16]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "reports/backend-quality-review/hard_negative_candidates.jsonl")
    parser.add_argument("--benchmark", type=Path, default=ROOT / "evaluations/real_matching_labels.json")
    parser.add_argument("--reviewer", help="Default reviewer name when a row has none.")
    parser.add_argument("--minimum-reviewed", type=int, default=1)
    args = parser.parse_args()
    reviewed = []
    seen = set()
    for line_number, line in enumerate(args.input.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        label = row.get("judgment")
        rationale = (row.get("rationale") or "").strip()
        reviewer = (row.get("reviewer") or args.reviewer or "").strip()
        reviewed_at = (row.get("reviewed_at") or "").strip()
        if label is None:
            continue
        if label not in {"same", "different", "unsure"} or not rationale or not reviewer or not reviewed_at:
            raise SystemExit(f"Line {line_number}: completed labels require same/different/unsure, rationale, reviewer, and reviewed_at.")
        identifier = pair_identity(row["title_a"], row["title_b"])
        if identifier in seen:
            raise SystemExit(f"Line {line_number}: duplicate reviewed pair {identifier}.")
        seen.add(identifier)
        reviewed.append({
            "id": identifier, "title_a": row["title_a"], "title_b": row["title_b"],
            "label": label, "confidence": "human_reviewed", "reason": rationale,
            "adjudicated_at": reviewed_at, "human_verified": True,
            "reviewer": reviewer, "category": "production_hard_negative_review",
            "split": "production_hard_negative_review",
        })
    if len(reviewed) < args.minimum_reviewed:
        raise SystemExit(f"Found {len(reviewed)} complete reviewed pairs; minimum is {args.minimum_reviewed}.")

    benchmark = json.loads(args.benchmark.read_text(encoding="utf-8"))
    existing = {pair["id"]: pair for pair in benchmark["pairs"]}
    for pair in reviewed:
        current = existing.get(pair["id"])
        if current and current["label"] != pair["label"]:
            raise SystemExit(f"Conflicting adjudication for {pair['id']}; resolve it explicitly before import.")
        existing[pair["id"]] = {**(current or {}), **pair}
    benchmark["pairs"] = sorted(existing.values(), key=lambda pair: pair["id"])
    benchmark["provenance"] = (
        "Human-verified production matcher stress labels plus individually reviewed hard-negative candidates. "
        "Selected challenge examples are not a random held-out estimate. Candidate predictions were hidden from reviewers."
    )
    atomic_text(args.benchmark, json.dumps(benchmark, indent=2, ensure_ascii=False) + "\n")
    print(f"Imported {len(reviewed)} reviewed pair(s); benchmark now contains {len(existing)} reviewed labels.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
