"""
Validation script for LLM judge against human seed labels.
Gate: >= 90% agreement or fail loudly.
Do not auto-tune the judge prompt.
"""
import json
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.llm.judge import run_llm_judge


def validate_judge(seed_file: Path = Path("data/judge_seed.jsonl")):
    if not seed_file.exists():
        print(f"Seed file {seed_file} does not exist.")
        sys.exit(1)

    records = []
    with open(seed_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))

    if not records:
        print(f"Seed file {seed_file} is empty. Please populate 20-30 hand-labeled pairs.")
        sys.exit(1)

    correct = 0
    total = len(records)
    print(f"Validating LLM Judge across {total} seed cases...")

    for r in records:
        expected = r.get("human_label")
        if not expected:
            continue
        judge_out = run_llm_judge(
            pair_id=r["pair_id"],
            title_a=r["title_a"],
            specs_a=r.get("specs_a", {}),
            title_b=r["title_b"],
            specs_b=r.get("specs_b", {})
        )
        pred = judge_out.get("label")
        if pred == expected:
            correct += 1
        else:
            print(f"[MISMATCH] pair_id={r['pair_id']}: Expected={expected}, Judge={pred} (Reason: {judge_out.get('reason')})")

    agreement = (correct / total) * 100.0 if total > 0 else 0.0
    print(f"Judge Agreement: {agreement:.2f}% ({correct}/{total})")

    if agreement < 90.0:
        print(f"FATAL GATE FAILURE: Judge agreement {agreement:.2f}% is below the mandatory 90% threshold!")
        sys.exit(1)

    print("GATE PASSED: Judge agreement >= 90%.")

if __name__ == "__main__":
    validate_judge()
