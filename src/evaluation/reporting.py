"""Reviewable Markdown alongside machine-readable, per-case JSON results."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def write_artifacts(report, output):
    atomic_text(output, json.dumps(report, indent=2, allow_nan=False) + "\n")
    atomic_text(Path(output).with_suffix(".md"), render_markdown(report))


def write_failure(output, error, report=None):
    if report is None:
        failure = {"evaluation_completed": False, "created_at": datetime.now(timezone.utc).isoformat(), "error": error}
        atomic_text(output, json.dumps(failure, indent=2) + "\n")
        atomic_text(Path(output).with_suffix(".md"), "# Backend evaluation failed\n\nNo passing quality result was produced.\n\n" + error + "\n")
    else:
        report["comparison_error"] = error
        write_artifacts(report, output)


def number(value):
    return "n/a" if value is None else f"{value:.4f}"


def render_markdown(report):
    summary = report["summary"]
    comparison = report.get("comparison")
    lines = ["# Backend quality report", "", report["scope"], "",
             f"- Cases: {summary['case_count']}",
             f"- Absolute correctness: **{'PASS' if summary['absolute_passed'] else 'FAIL'}**",
             f"- Failed checks: {summary['checks']['failed']} ({summary['checks']['critical_failed']} critical)",
             f"- Dataset: `{report['dataset_sha256']}`", f"- Catalog: `{report['catalog_sha256']}`",
             f"- Source: `{report['source_sha256']}`", ""]
    if comparison:
        lines += [f"- Baseline regression gate: **{'PASS' if comparison['passed'] else 'FAIL'}**",
                  f"- Regressions: {len(comparison['regressions'])}; improvements: {len(comparison['improvements'])}",
                  "", comparison["gate_policy"], "",
                  "A passing baseline gate can coexist with known correctness failures. It does not certify production accuracy.", ""]
    if report.get("comparison_error"):
        lines += ["**Baseline gate: NOT COMPARABLE / FAILED**", "", report["comparison_error"], ""]
    if comparison and comparison.get("search_scorecard"):
        lines += ["## Search changes against baseline", "",
                  "| Surface / metric | Baseline | Current | Delta |",
                  "| --- | --- | --- | --- |"]
        for surface, scores in comparison["search_scorecard"].items():
            for metric, values in scores.items():
                before, after = values["before"], values["after"]
                delta = None if before is None or after is None else after - before
                lines.append(f"| {surface}/{metric} | {number(before)} | {number(after)} | {number(delta)} |")
        lines.append("")
    lines += ["## Search (macro average over queries)", "",
              "Ranking uses distinct product entities, so duplicate vendor offers earn no additional relevance credit. P@5 divides by five even when fewer results exist. Empty-answer queries use abstention checks and are excluded from positive-query ranking denominators.", "",
              "| Surface | Answerable / empty queries | P@5 | Returned P@5 | Recall@50 | F1@50 | nDCG@10 | MRR | False-empty rate | Correct abstention |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for surface, row in summary["search_by_surface"].items():
        values = [surface, f"{row['answerable_queries']} / {row['empty_answer_queries']}"]
        values += [number(row[key]) for key in ("precision_at_5", "returned_precision_at_5", "recall_at_50", "f1_at_50", "ndcg_at_10", "mrr", "false_empty_rate", "correct_abstention_rate")]
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", "## Matching", "",
              "Historical dev/held_out labels are regression collections, not a claim of a newly blinded experiment. Precision/recall/F1 describe completed decisions; errors reduce decision coverage and correct-case rate and fail the gate. Undefined precision or recall is n/a.", "",
              "| Pipeline / split | Decisions / cases | Errors | TP | FP | TN | FN | Precision | Recall | F1 |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, row in summary["matching"].items():
        values = [name, f"{row['support']} / {row['total_cases']}", str(row["execution_errors"])] + [str(row[key]) for key in ("tp", "fp", "tn", "fn")]
        values += [number(row[key]) for key in ("precision", "recall", "f1")]
        lines.append("| " + " | ".join(values) + " |")
    groups = summary["grouping"]
    pairwise = groups["pairwise"]
    lines += ["", "## Final comparison groups", "",
              f"{groups['correct']} / {groups['scenarios']} complete scenarios correct; {groups['execution_errors']} execution errors.",
              f"Pairwise precision: {number(pairwise['precision'])}; recall: {number(pairwise['recall'])}; F1: {number(pairwise['f1'])}. "
              f"TP={pairwise['tp']}, FP={pairwise['fp']}, TN={pairwise['tn']}, FN={pairwise['fn']}.",
              "Execution errors are recorded separately and fail the gate; they are never presented as successfully evaluated groups.", ""]
    safety = summary["safety"]
    lines += ["## Constraint and comparison safety", "",
              f"Queries returning an ineligible entity/offer or violating serialized price/stock/vendor bounds: "
              f"{safety['queries_returning_ineligible_entities']} / {safety['successful_query_support']} successful queries "
              f"(rate {number(safety['successful_query_constraint_violation_rate'])}).",
              f"Incompatible comparison-group rate: {number(safety['incompatible_group_rate'])} over {safety['returned_comparison_groups']} returned groups.", ""]
    lines += ["", "## Failed cases", ""]
    failures = [case for case in report["cases"] if any(not c["passed"] for c in case["checks"])]
    if not failures:
        lines += ["None.", ""]
    for case in failures:
        lines += [f"### {case['id']}", ""]
        if case.get("rationale"):
            lines += [case["rationale"], ""]
        for check in case["checks"]:
            if not check["passed"]:
                detail = check.get("violations") or check.get("details") or case.get("error") or case.get("reason") or "failed"
                lines.append(f"- {'CRITICAL' if check['critical'] else 'QUALITY'} `{check['name']}`: {detail}")
        if case.get("returned_entities") is not None:
            lines.append(f"- Returned entities: `{case['returned_entities']}`")
        lines.append("")
    if comparison:
        for title, key in (("Regressions", "regressions"), ("Improvements", "improvements")):
            lines += [f"## {title}", ""]
            lines += [f"- `{entry['case']}`: {entry.get('metric', entry['kind'])}" +
                      (f" {number(entry['before'])} → {number(entry['after'])}" if entry["kind"] == "query_metric" else "")
                      for entry in comparison[key]] or ["None."]
            lines.append("")
        lines += ["## Paired query bootstrap intervals", "",
                  "Descriptive 95% intervals for mean metric deltas, resampling paired queries with a fixed seed. The strict per-query regression gate does not depend on significance or latency noise.", "",
                  "| Surface / metric | Queries | Mean delta | 95% interval |", "| --- | --- | --- | --- |"]
        for name, entry in comparison["paired_query_intervals"].items():
            interval = entry["ci95"]
            lines.append(f"| {name} | {entry['support']} | {number(entry['mean_delta'])} | {number(interval[0])}, {number(interval[1])} |")
        lines.append("")
    return "\n".join(lines)
