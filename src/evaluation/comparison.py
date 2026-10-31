"""Baseline compatibility and regression gates. Dataset changes require rebaselining."""
from __future__ import annotations

import math

from .metrics import RANKING_METRICS, paired_bootstrap


def validate_score(value, context):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"Invalid metric value for {context}.")


def failure_keys(report):
    keys = set()
    for case in report["cases"]:
        for check in case["checks"]:
            if not check["passed"]:
                prefix = f"{case['id']}::{check['name']}"
                # A check already failing for one listing must still detect a
                # newly violating listing or an additional incompatible pair.
                keys.update(f"{prefix}::{item}" for item in check.get("violations", []) or ["failed"])
    return keys


def compare_reports(current, baseline, *, strict=False):
    for key in ("report_version", "metric_version", "dataset_sha256", "catalog_sha256", "evaluator_sha256", "suite_config"):
        if current.get(key) != baseline.get(key):
            raise ValueError(f"Baseline {key} differs. Compare identical datasets/catalogs/configuration, or explicitly establish a new baseline.")
    previous = {case["id"]: case for case in baseline["cases"]}
    now = {case["id"]: case for case in current["cases"]}
    if len(previous) != len(baseline["cases"]) or len(now) != len(current["cases"]) or previous.keys() != now.keys():
        raise ValueError("Baseline case identities differ or contain duplicates.")
    regressions, improvements, paired = [], [], {}
    old_failures, new_failures = failure_keys(baseline), failure_keys(current)
    regressions.extend({"kind": "new_check_failure", "case": key} for key in sorted(new_failures - old_failures))
    improvements.extend({"kind": "resolved_check_failure", "case": key} for key in sorted(old_failures - new_failures))
    # Each query and slice is protected, so gains elsewhere cannot hide a loss.
    for identifier, case in now.items():
        old = previous[identifier]
        if any(case.get(key) != old.get(key) for key in ("kind", "surface", "category")):
            raise ValueError(f"Baseline case definition differs for {identifier}.")
        if case["kind"] == "search":
            for metric in RANKING_METRICS:
                before, after = old["metrics"][metric], case["metrics"][metric]
                if (before is None) != (after is None):
                    raise ValueError(f"Metric denominator changed for {identifier}/{metric}.")
                if before is None:
                    continue
                for value in (before, after):
                    validate_score(value, f"{identifier}/{metric}")
                change = after - before
                paired.setdefault(f"{case['surface']}/{metric}", []).append(change)
                if abs(change) > 1e-12:
                    entry = {"kind": "query_metric", "case": identifier, "metric": metric,
                             "before": before, "after": after, "delta": change}
                    (regressions if change < 0 else improvements).append(entry)
        elif case["kind"] in {"matching", "grouping"} and old["correct"] != case["correct"]:
            entry = {"kind": case["kind"], "case": identifier,
                     "before": old["correct"], "after": case["correct"]}
            (improvements if case["correct"] else regressions).append(entry)
        elif case["kind"] == "pagination":
            before, after = old["coverage"], case["coverage"]
            for value in (before, after):
                validate_score(value, f"{identifier}/coverage")
            if abs(after - before) > 1e-12:
                entry = {"kind": "pagination_coverage", "case": identifier, "before": before, "after": after}
                (improvements if after > before else regressions).append(entry)
    if strict:
        regressions.extend({"kind": "strict_check_failure", "case": key} for key in sorted(new_failures))
    return {
        "passed": not regressions, "strict": strict,
        "baseline_failures": len(old_failures), "current_failures": len(new_failures),
        "regressions": regressions, "improvements": improvements,
        "search_scorecard": {
            surface: {metric: {"before": baseline["summary"]["search_by_surface"][surface][metric],
                               "after": row[metric]}
                      for metric in RANKING_METRICS}
            for surface, row in current.get("summary", {}).get("search_by_surface", {}).items()
        },
        "paired_query_intervals": {key: paired_bootstrap(values) for key, values in sorted(paired.items())},
        "gate_policy": "No new failed checks, no per-query quality decrease, no previously correct matching/grouping case becoming incorrect. Latency is informational.",
    }
