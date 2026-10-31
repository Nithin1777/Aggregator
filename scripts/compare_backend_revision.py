"""Compare the working backend with a Git revision under identical judgments.

The reference is extracted into a disposable checkout. Both implementations run
with today's evaluator, cases, and fixture facts. No worktree reset, production
catalog copy, vendor request, or dependency installation is performed.

python scripts/compare_backend_revision.py --ref HEAD
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.evaluation.comparison import compare_reports
from src.evaluation.reporting import atomic_text, write_artifacts, write_failure


def copy_evaluator(checkout):
    for relative in ("src/evaluation", "evaluations"):
        target = checkout / relative
        if target.exists():
            if not target.resolve().is_relative_to(checkout.resolve()):
                raise ValueError("Evaluation copy target escaped the disposable checkout.")
            shutil.rmtree(target)
        shutil.copytree(ROOT / relative, target, ignore=shutil.ignore_patterns("__pycache__", "baseline.json"))
    for relative in ("scripts/evaluate_backend.py", "tests/data/matching_golden_set.json"):
        target = checkout / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def capture(checkout, output):
    command = [sys.executable, str(checkout / "scripts/evaluate_backend.py"),
               "--write-baseline", str(output.with_suffix(".capture.json")),
               "--reason", "Ephemeral capture for a paired Git revision comparison", "--output", str(output)]
    result = subprocess.run(command, cwd=checkout, capture_output=True, text=True, encoding="utf-8", timeout=300)
    if result.returncode:
        raise RuntimeError(f"Reference/candidate evaluation failed in {checkout}:\n{result.stdout}\n{result.stderr}")
    return json.loads(output.read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ref", required=True, help="Commit, branch or tag to measure as the reference.")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/backend-quality/revision.json")
    parser.add_argument("--strict", action="store_true", help="Also fail existing correctness debt.")
    args = parser.parse_args()
    if args.output.resolve() == (ROOT / "evaluations/baseline.json").resolve():
        parser.error("The comparison report must not overwrite the versioned baseline.")
    try:
        commit = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", args.ref + "^{commit}"], cwd=ROOT, text=True, stderr=subprocess.PIPE).strip()
        with tempfile.TemporaryDirectory(prefix="aggregate_reference_") as directory:
            temporary = Path(directory)
            archive, checkout = temporary / "reference.tar", temporary / "reference"
            checkout.mkdir()
            with archive.open("wb") as stream:
                subprocess.run(["git", "archive", "--format=tar", commit], cwd=ROOT, stdout=stream, check=True)
            with tarfile.open(archive) as source:
                source.extractall(checkout, filter="data")
            copy_evaluator(checkout)
            reference = capture(checkout, temporary / "reference.json")
            candidate = capture(ROOT, temporary / "candidate.json")
            candidate["comparison"] = compare_reports(candidate, reference, strict=args.strict)
            candidate["reference_commit"] = commit
            reference["git_commit"] = commit
            write_artifacts(candidate, args.output)
            reference_path = args.output.with_name(args.output.stem + "-reference.json")
            atomic_text(reference_path, json.dumps(reference, indent=2, allow_nan=False) + "\n")
        comparison = candidate["comparison"]
        print(f"Compared working backend with {commit[:12]}: {'PASS' if comparison['passed'] else 'FAIL'}")
        print(f"{len(comparison['regressions'])} regressions; {len(comparison['improvements'])} improvements.")
        print(f"Absolute correctness: {'PASS' if candidate['summary']['absolute_passed'] else 'FAIL'}")
        print(f"Report: {args.output.with_suffix('.md').resolve()}")
        return 0 if comparison["passed"] else 1
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        write_failure(args.output, error)
        print(f"Revision comparison could not complete: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
