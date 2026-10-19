#!/usr/bin/env python3
"""
LLM Resolver: I/O and validation harness bridge for external agent labeling.
Subcommands:
  --extract : Writes data/not_sure.jsonl and data/NOT_SURE_PROMPT.md
  --collect : Validates external agent output data/not_sure_classified.jsonl and appends to data/resolver_output.jsonl

NO LLM API CALLS ARE MADE. The external harness acts as the runtime.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.llm.prompt_template import get_prompt_text, get_rubric_hash

DATA_DIR = REPO_ROOT / "data"
ARCHIVE_DIR = DATA_DIR / "resolver_archive"


def compute_pair_id(title_a: str, title_b: str) -> str:
    """Canonical content-addressed pair ID: sha1(sorted(lower(a), lower(b)))[:16]."""
    norm = sorted([title_a.lower().strip(), title_b.lower().strip()])
    return hashlib.sha1("::".join(norm).encode("utf-8")).hexdigest()[:16]


def load_cached_pair_ids(resolver_output_path: Path) -> set:
    """Returns set of pair_ids already collected in resolver_output.jsonl."""
    cached = set()
    if resolver_output_path.exists():
        with open(resolver_output_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if "pair_id" in obj:
                        cached.add(obj["pair_id"])
                except Exception:
                    continue
    return cached


def run_extract(args):
    """Generates not_sure.jsonl and NOT_SURE_PROMPT.md for harness."""
    input_path = Path(args.input) if args.input else DATA_DIR / "gray_pairs.jsonl"
    out_dir = Path(args.output) if args.output else DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    not_sure_file = out_dir / "not_sure.jsonl"
    prompt_file = out_dir / "NOT_SURE_PROMPT.md"
    hash_file = out_dir / "not_sure_prompt_hash.txt"
    resolver_output_file = out_dir / "resolver_output.jsonl"

    if not input_path.exists():
        print(f"Error: Input file {input_path} does not exist.", file=sys.stderr)
        sys.exit(1)

    cached_ids = set()
    if not args.force:
        cached_ids = load_cached_pair_ids(resolver_output_file)

    pairs = []
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            # Ensure pair_id matches sha1 scheme
            t_a = rec.get("title_a", "")
            t_b = rec.get("title_b", "")
            pid = rec.get("pair_id")
            if not pid or pid.startswith("gz_"):
                pid = compute_pair_id(t_a, t_b)

            if not args.force and pid in cached_ids:
                continue

            pairs.append({
                "pair_id": pid,
                "title_a": t_a,
                "specs_a": rec.get("specs_a", {}),
                "title_b": t_b,
                "specs_b": rec.get("specs_b", {})
            })

    # Range / slice filtering
    if args.start is not None or args.end is not None:
        start_idx = args.start if args.start is not None else 0
        end_idx = args.end if args.end is not None else len(pairs)
        pairs = pairs[start_idx:end_idx]

    if args.limit and args.limit > 0:
        pairs = pairs[:args.limit]

    # Write not_sure.jsonl
    with open(not_sure_file, "w", encoding="utf-8") as f:
        for p in pairs:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")

    # Write NOT_SURE_PROMPT.md
    classified_rel = "not_sure_classified.jsonl" if out_dir == DATA_DIR else str(out_dir / "not_sure_classified.jsonl")
    input_rel = "not_sure.jsonl" if out_dir == DATA_DIR else str(out_dir / "not_sure.jsonl")
    prompt_text = get_prompt_text(input_file=input_rel, output_file=classified_rel)
    with open(prompt_file, "w", encoding="utf-8") as f:
        f.write(prompt_text)

    # Store rubric hash
    rubric_hash = get_rubric_hash()
    with open(hash_file, "w", encoding="utf-8") as f:
        f.write(rubric_hash + "\n")

    print(f"Wrote {len(pairs)} pairs to {not_sure_file}")
    print(f"Wrote instructions to {prompt_file}")
    print(f"Rubric hash: {rubric_hash}")
    print("NEXT: point your agent harness at data/ and say:")
    print(" 'Read NOT_SURE_PROMPT.md, classify not_sure.jsonl, write not_sure_classified.jsonl'")
    print("Then run: python src/llm/resolver.py --collect")


def clean_harness_lines(filepath: Path):
    """Reads file, stripping markdown code block fences and blank lines."""
    with open(filepath, "r", encoding="utf-8") as f:
        raw_lines = f.readlines()

    cleaned = []
    for line in raw_lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("```"):
            continue
        cleaned.append(s)
    return cleaned


def run_collect(args):
    """Validates external harness output and appends to resolver_output.jsonl."""
    out_dir = Path(args.output) if args.output else DATA_DIR
    not_sure_file = out_dir / "not_sure.jsonl"
    classified_file = out_dir / "not_sure_classified.jsonl"
    prompt_file = out_dir / "NOT_SURE_PROMPT.md"
    resolver_output_file = out_dir / "resolver_output.jsonl"

    if not not_sure_file.exists():
        print(f"Error: Expected input pairs file {not_sure_file} does not exist.", file=sys.stderr)
        sys.exit(1)
    if not classified_file.exists():
        print(f"Error: Harness output file {classified_file} does not exist.", file=sys.stderr)
        sys.exit(1)

    # Read expected pairs
    expected_pairs = []
    with open(not_sure_file, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            expected_pairs.append(json.loads(line))

    # Read and clean classified lines
    raw_lines = clean_harness_lines(classified_file)

    # 1. Row count check
    if len(raw_lines) != len(expected_pairs):
        print(f"Row count mismatch: expected {len(expected_pairs)}, got {len(raw_lines)}", file=sys.stderr)
        sys.exit(1)

    # 2. Schema and strict validation per line
    valid_labels = {"same", "different", "unsure"}
    valid_confidences = {"high", "medium", "low"}
    validated_records = []

    for line_num, (line_str, exp) in enumerate(zip(raw_lines, expected_pairs), 1):
        try:
            record = json.loads(line_str)
        except Exception as e:
            print(f"Line {line_num}: JSON parse error: {e}", file=sys.stderr)
            sys.exit(1)

        pid = record.get("pair_id")
        exp_pid = exp.get("pair_id")
        if pid != exp_pid:
            print(f"Line {line_num}: pair_id mismatch. Expected {exp_pid}, got {pid}", file=sys.stderr)
            sys.exit(1)

        label = record.get("label", "").lower().strip()
        if label not in valid_labels:
            print(f"Line {line_num}: invalid label '{label}' — must be same|different|unsure", file=sys.stderr)
            sys.exit(1)

        confidence = record.get("confidence", "").lower().strip()
        if confidence not in valid_confidences:
            print(f"Line {line_num}: invalid confidence '{confidence}' — must be high|medium|low", file=sys.stderr)
            sys.exit(1)

        record["label"] = label
        record["confidence"] = confidence
        record["title_a"] = exp.get("title_a", "")
        record["title_b"] = exp.get("title_b", "")
        record["resolved_at"] = datetime.now(timezone.utc).isoformat()
        validated_records.append(record)

    # 3. Append to resolver_output.jsonl (dedup by pair_id)
    existing_output = {}
    if resolver_output_file.exists():
        with open(resolver_output_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    item = json.loads(line)
                    existing_output[item["pair_id"]] = item

    for r in validated_records:
        existing_output[r["pair_id"]] = r

    with open(resolver_output_file, "w", encoding="utf-8") as f:
        for item in existing_output.values():
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # 4. Archive files
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_extract = ARCHIVE_DIR / f"extract_{ts}.jsonl"
    archive_classified = ARCHIVE_DIR / f"classified_{ts}.jsonl"
    archive_prompt = ARCHIVE_DIR / f"prompt_{ts}.md"

    shutil.move(str(not_sure_file), str(archive_extract))
    shutil.move(str(classified_file), str(archive_classified))
    if prompt_file.exists():
        shutil.move(str(prompt_file), str(archive_prompt))

    print(f"Collected {len(validated_records)} pairs -> {resolver_output_file}")


def main():
    parser = argparse.ArgumentParser(description="LLM Resolver for external agent harness.")
    parser.add_argument("--extract", action="store_true", help="Extract pending gray pairs and generate prompt instructions.")
    parser.add_argument("--collect", action="store_true", help="Validate harness classifications and append to resolver_output.jsonl.")
    parser.add_argument("--input", type=str, default=None, help="Input file path.")
    parser.add_argument("--output", type=str, default=None, help="Output directory path.")
    parser.add_argument("--limit", type=int, default=None, help="Cap to N pairs.")
    parser.add_argument("--start", type=int, default=None, help="Start index for slicing.")
    parser.add_argument("--end", type=int, default=None, help="End index for slicing.")
    parser.add_argument("--force", action="store_true", help="Reprocess even cached pairs.")

    args = parser.parse_args()

    if args.extract:
        run_extract(args)
    elif args.collect:
        run_collect(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
