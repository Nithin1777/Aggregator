"""Run the offline backend quality suite and compare with a reviewed baseline.

python scripts/evaluate_backend.py
python scripts/evaluate_backend.py --strict
python scripts/evaluate_backend.py --write-baseline evaluations/baseline.json --reason "Reviewed policy change"
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.evaluation.comparison import compare_reports
from src.evaluation.reporting import atomic_text, write_artifacts, write_failure
from src.evaluation.runner import run_suite


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=ROOT / "evaluations")
    parser.add_argument("--baseline", type=Path, default=ROOT / "evaluations/baseline.json")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/backend-quality/latest.json")
    parser.add_argument("--strict", action="store_true", help="Fail on every remaining failed check, including baseline debt.")
    parser.add_argument("--write-baseline", type=Path, help="Explicitly establish or replace a baseline; inspect the resulting report before committing.")
    parser.add_argument("--reason", help="Required explanation when establishing a baseline.")
    args = parser.parse_args()
    if args.write_baseline and not (args.reason or "").strip():
        parser.error("--write-baseline requires a nonempty --reason.")
    protected = args.write_baseline or args.baseline
    if args.output.resolve() == protected.resolve() or args.output.with_suffix(".md").resolve() == protected.resolve():
        parser.error("The report output must not overwrite the baseline.")
    report = None
    try:
        if not args.write_baseline and not args.baseline.is_file():
            raise ValueError(f"Baseline not found: {args.baseline}. Establish one explicitly with --write-baseline and --reason.")
        report = asyncio.run(run_suite(args.dataset.resolve()))
        if args.write_baseline:
            report["baseline_note"] = args.reason.strip()
            atomic_text(args.write_baseline, json.dumps(report, indent=2, allow_nan=False) + "\n")
            passed = report["summary"]["absolute_passed"] if args.strict else True
        else:
            baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
            report["comparison"] = compare_reports(report, baseline, strict=args.strict)
            passed = report["comparison"]["passed"]
        write_artifacts(report, args.output)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        write_failure(args.output, error, report)
        print(f"Evaluation could not complete: {error}", file=sys.stderr)
        return 2
    summary = report["summary"]
    action = "Baseline recorded" if args.write_baseline else f"Regression gate: {'PASS' if passed else 'FAIL'}"
    print(f"{action}. Absolute correctness: {'PASS' if summary['absolute_passed'] else 'FAIL'}.")
    print(f"{summary['case_count']} cases; {summary['checks']['failed']} failed checks, {summary['checks']['critical_failed']} critical.")
    if report.get("comparison"):
        print(f"{len(report['comparison']['regressions'])} regressions; {len(report['comparison']['improvements'])} improvements.")
    print(f"JSON: {args.output.resolve()}\nReport: {args.output.with_suffix('.md').resolve()}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
