#!/usr/bin/env python3
"""
CLI labeling tool for human evaluation of gray-zone component match candidate pairs.
Reads candidate pairs from data/gray_sample.jsonl and outputs human ground-truth labels
to data/judge_seed.jsonl.

Resumable: Skips pairs already present in data/judge_seed.jsonl.
DO NOT execute autonomously — for human annotator use.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
INPUT_FILE = DATA_DIR / "gray_sample.jsonl"
OUTPUT_FILE = DATA_DIR / "judge_seed.jsonl"

LABEL_MAP = {
    "1": "same",
    "2": "variant",
    "3": "different",
    "4": "unsure",
}

def load_existing_labeled_ids(output_path: Path) -> set:
    """Returns set of pair_ids already labeled in output_path."""
    if not output_path.exists():
        return set()
    labeled = set()
    with open(output_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "pair_id" in rec:
                    labeled.add(rec["pair_id"])
            except Exception:
                continue
    return labeled

def main():
    if not INPUT_FILE.exists():
        print(f"Error: Input file {INPUT_FILE} does not exist.", file=sys.stderr)
        sys.exit(1)

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        pairs = [json.loads(line) for line in f if line.strip()]

    labeled_ids = load_existing_labeled_ids(OUTPUT_FILE)
    remaining_pairs = [p for p in pairs if p.get("pair_id") not in labeled_ids]

    print("=" * 70)
    print("Electronic Component Matching - Human Judge Seed Labeler")
    print("=" * 70)
    print(f"Total candidate pairs: {len(pairs)}")
    print(f"Already labeled:       {len(labeled_ids)}")
    print(f"Remaining to label:    {len(remaining_pairs)}")
    print("Options for each pair:")
    print("  [1] same       - Identical component offering (same part/specs)")
    print("  [2] variant    - Same family/chip but differing specs (voltage/channels/pinout)")
    print("  [3] different  - Distinct parts or incompatible modules/accessories")
    print("  [4] unsure     - Ambiguous, insufficient info to confirm")
    print("  [s] skip       - Skip this pair without labeling")
    print("  [q] quit       - Save and exit immediately")
    print("=" * 70)

    if not remaining_pairs:
        print("All pairs have already been labeled in judge_seed.jsonl!")
        return

    count_labeled_session = 0
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(OUTPUT_FILE, "a", encoding="utf-8") as out_fp:
        for idx, pair in enumerate(remaining_pairs, start=1):
            pair_id = pair.get("pair_id", f"pair_{idx}")
            title_a = pair.get("title_a", "N/A")
            title_b = pair.get("title_b", "N/A")
            mpn_a = pair.get("mpn_a") or pair.get("specs_a", {}).get("mpn", "None")
            mpn_b = pair.get("mpn_b") or pair.get("specs_b", {}).get("mpn", "None")
            price_a = pair.get("price_a", "N/A")
            price_b = pair.get("price_b", "N/A")

            print(f"\n--- [Pair {idx}/{len(remaining_pairs)}] ID: {pair_id} ---")
            print(f"  [A] Title: {title_a}")
            print(f"      MPN:   {mpn_a} | Price: {price_a}")
            print(f"  [B] Title: {title_b}")
            print(f"      MPN:   {mpn_b} | Price: {price_b}")

            while True:
                choice = input("\nLabel [1:same / 2:variant / 3:different / 4:unsure / s:skip / q:quit]: ").strip().lower()
                if choice == "q":
                    print(f"\nExiting session. Saved {count_labeled_session} new labels to {OUTPUT_FILE}.")
                    return
                if choice == "s":
                    print("Skipped.")
                    break
                if choice in LABEL_MAP:
                    label = LABEL_MAP[choice]
                    notes = ""
                    if choice == "4":
                        notes = input("Optional note on uncertainty: ").strip()

                    record = {
                        "pair_id": pair_id,
                        "title_a": title_a,
                        "title_b": title_b,
                        "label": label,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "notes": notes
                    }
                    out_fp.write(json.dumps(record) + "\n")
                    out_fp.flush()
                    count_labeled_session += 1
                    print(f"-> Recorded: '{label}'")
                    break
                print("Invalid selection. Please choose 1, 2, 3, 4, s, or q.")

    print(f"\nCompleted! Saved {count_labeled_session} new labels to {OUTPUT_FILE}.")

if __name__ == "__main__":
    main()
