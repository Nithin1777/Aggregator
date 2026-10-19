#!/usr/bin/env python3
"""
Offline validation of resolver decisions against ground-truth human seed labels.
Joins resolver output to the seed by sorted lowercased (title_a, title_b),
NOT by pair_id, to bridge legacy gz_XXXXX IDs with canonical sha1 IDs.

Gate: agreement >= 85% to pass.
"""
import argparse
import json
from pathlib import Path
import sys

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def normalize_title_pair(title_a: str, title_b: str):
    return tuple(sorted([title_a.lower().strip(), title_b.lower().strip()]))


def validate_resolver_against_seed(input_file: Path, seed_file: Path):
    if not input_file.exists():
        print(f"Error: Input file {input_file} does not exist.", file=sys.stderr)
        sys.exit(1)
    if not seed_file.exists():
        print(f"Error: Seed file {seed_file} does not exist.", file=sys.stderr)
        sys.exit(1)

    # 1. Load seed labels
    seed_by_title = {}
    seed_by_pid = {}
    with open(seed_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            t_a = rec.get("title_a", "")
            t_b = rec.get("title_b", "")
            pid = rec.get("pair_id")
            expected_label = rec.get("human_label") or rec.get("label")
            if expected_label:
                entry = {
                    "pair_id": pid,
                    "title_a": t_a,
                    "title_b": t_b,
                    "human_label": expected_label.lower().strip()
                }
                if t_a and t_b:
                    seed_by_title[normalize_title_pair(t_a, t_b)] = entry
                if pid:
                    seed_by_pid[pid] = entry

    print(f"Loaded {len(seed_by_title)} ground-truth seed pairs from {seed_file}.")

    # 2. Load resolver output
    resolver_records = []
    with open(input_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            resolver_records.append(json.loads(line))

    print(f"Loaded {len(resolver_records)} resolver predictions from {input_file}.")

    # 3. Join by normalized title pair (priority) or pair_id
    correct = 0
    mismatches = []
    joined_count = 0
    seen_seeds = set()

    for pred in resolver_records:
        t_a = pred.get("title_a", "")
        t_b = pred.get("title_b", "")
        pid = pred.get("pair_id")

        seed_entry = None
        if t_a and t_b:
            key = normalize_title_pair(t_a, t_b)
            seed_entry = seed_by_title.get(key)
        if not seed_entry and pid:
            seed_entry = seed_by_pid.get(pid)

        if not seed_entry:
            continue

        seed_key = seed_entry["pair_id"] or (seed_entry["title_a"], seed_entry["title_b"])
        if seed_key in seen_seeds:
            continue
        seen_seeds.add(seed_key)

        joined_count += 1
        expected = seed_entry["human_label"]
        predicted = pred.get("label", "").lower().strip()

        if expected == predicted:
            correct += 1
        else:
            mismatches.append({
                "pair_id": seed_entry["pair_id"],
                "expected": expected,
                "predicted": predicted,
                "reason": pred.get("reason"),
                "title_a": seed_entry["title_a"][:45],
                "title_b": seed_entry["title_b"][:45]
            })

    total = joined_count
    agreement = (correct / total) * 100.0 if total > 0 else 0.0

    print("=" * 60)
    print("RESOLVER OFFLINE SEED VALIDATION REPORT")
    print("=" * 60)
    print(f"Joined Pairs:   {total}")
    print(f"Agreements:     {correct}")
    print(f"Disagreements:  {len(mismatches)}")
    print(f"Agreement Rate: {agreement:.2f}% ({correct}/{total})")

    if mismatches:
        print("\nMismatches:")
        for m in mismatches[:5]:
            print(f"  [{m['pair_id']}] Expected='{m['expected']}' vs Pred='{m['predicted']}' (Reason: {m['reason']})")

    print("=" * 60)

    if agreement < 85.0:
        print(f"FATAL GATE FAILURE: Agreement {agreement:.2f}% is below 85.0% threshold!", file=sys.stderr)
        sys.exit(1)

    print("GATE PASSED: Agreement >= 85.0%.")
    return agreement


def main():
    parser = argparse.ArgumentParser(description="Validate resolver output against ground-truth seed labels.")
    parser.add_argument("--input", type=str, default=str(DATA_DIR / "resolver_output.jsonl"), help="Resolver output JSONL path.")
    parser.add_argument("--seed", type=str, default=str(DATA_DIR / "judge_seed.jsonl"), help="Ground truth seed JSONL path.")

    args = parser.parse_args()
    validate_resolver_against_seed(Path(args.input), Path(args.seed))


if __name__ == "__main__":
    main()
