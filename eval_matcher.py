"""
Matching Engine Evaluation & Benchmarking Harness.
Evaluates precision, recall, F1, and category-by-category metrics
against Dev and Held-Out evaluation splits.
"""
import json
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from rich.console import Console
from rich.table import Table

from normalizer import (
    calculate_token_similarity,
    extract_mpn,
    get_canonical_cluster_key,
    match_components_v2,
    normalize_title,
)

console = Console()
BENCHMARK_DATA_PATH = Path(__file__).parent / "tests" / "data" / "matching_golden_set.json"


def baseline_matcher(title_a: str, title_b: str) -> Tuple[bool, float, str]:
    """
    Baseline V1 matching logic:
    1. Canonical cluster key match (basic regex MPN or top-4 tokens).
    2. Fallback to Jaccard token similarity with 0.70 threshold.
    """
    key_a = get_canonical_cluster_key(title_a)
    key_b = get_canonical_cluster_key(title_b)

    if key_a == key_b:
        return True, 1.0, f"Canonical cluster key match: {key_a}"

    norm_a = normalize_title(title_a)
    norm_b = normalize_title(title_b)
    sim = calculate_token_similarity(norm_a, norm_b)

    if sim >= 0.70:
        return True, sim, f"Jaccard token similarity {sim:.3f} >= 0.70"

    return False, sim, f"Below threshold: {sim:.3f} < 0.70 (keys: {key_a} vs {key_b})"


def evaluate_matcher(
    matcher_func: Callable[[str, str], Tuple[bool, float, str]],
    matcher_name: str = "Matcher Evaluation",
    split: Optional[str] = None  # 'dev', 'held_out', or None for all
) -> Dict[str, any]:
    """
    Runs the benchmark against the golden dataset and computes overall and per-category metrics.
    """
    if not BENCHMARK_DATA_PATH.exists():
        raise FileNotFoundError(f"Golden dataset not found at {BENCHMARK_DATA_PATH}")

    with open(BENCHMARK_DATA_PATH, "r", encoding="utf-8") as f:
        all_cases = json.load(f)

    # Filter by split if requested
    if split:
        cases = [c for c in all_cases if c.get("split") == split]
    else:
        cases = all_cases

    tp, fp, tn, fn = 0, 0, 0, 0
    # category -> {total, tp, fp, tn, fn}
    category_stats: Dict[str, Dict[str, int]] = {}
    failures = []

    for case in cases:
        cid = case["id"]
        cat = case["category"]
        ta = case["title_a"]
        tb = case["title_b"]
        expected = case["should_match"]

        if cat not in category_stats:
            category_stats[cat] = {"total": 0, "tp": 0, "fp": 0, "tn": 0, "fn": 0}
        category_stats[cat]["total"] += 1

        is_match, score, reason = matcher_func(ta, tb)

        if expected and is_match:
            tp += 1
            category_stats[cat]["tp"] += 1
        elif not expected and not is_match:
            tn += 1
            category_stats[cat]["tn"] += 1
        elif not expected and is_match:
            fp += 1  # False Positive: BAD MERGE
            category_stats[cat]["fp"] += 1
            failures.append({
                "id": cid,
                "split": case.get("split", "unknown"),
                "category": cat,
                "error_type": "FALSE POSITIVE (BAD MERGE)",
                "title_a": ta,
                "title_b": tb,
                "expected": expected,
                "actual": is_match,
                "score": score,
                "reason": reason,
                "description": case.get("description", "")
            })
        elif expected and not is_match:
            fn += 1  # False Negative: MISSED MERGE
            category_stats[cat]["fn"] += 1
            failures.append({
                "id": cid,
                "split": case.get("split", "unknown"),
                "category": cat,
                "error_type": "FALSE NEGATIVE (MISSED MERGE)",
                "title_a": ta,
                "title_b": tb,
                "expected": expected,
                "actual": is_match,
                "score": score,
                "reason": reason,
                "description": case.get("description", "")
            })

    total = len(cases)
    accuracy = (tp + tn) / total if total > 0 else 0.0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

    # Calculate per-category precision & recall
    per_cat_metrics = {}
    for cat, s in category_stats.items():
        c_p = s["tp"] / (s["tp"] + s["fp"]) if (s["tp"] + s["fp"]) > 0 else (1.0 if s["fp"] == 0 else 0.0)
        c_r = s["tp"] / (s["tp"] + s["fn"]) if (s["tp"] + s["fn"]) > 0 else (1.0 if s["fn"] == 0 else 0.0)
        c_f1 = (2 * c_p * c_r) / (c_p + c_r) if (c_p + c_r) > 0 else 0.0
        per_cat_metrics[cat] = {
            "total": s["total"],
            "tp": s["tp"],
            "fp": s["fp"],
            "tn": s["tn"],
            "fn": s["fn"],
            "precision": c_p,
            "recall": c_r,
            "f1": c_f1,
            "passed": s["tp"] + s["tn"]
        }

    return {
        "name": matcher_name,
        "split": split or "all",
        "total": total,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "categories": per_cat_metrics,
        "failures": failures
    }


def print_evaluation_report(results: Dict[str, any]):
    """Renders detailed table of overall and per-category precision/recall."""
    split_tag = f" [Split: {results['split'].upper()}]" if results.get("split") else ""
    console.print(f"\n[bold cyan]================================================================[/bold cyan]")
    console.print(f"[bold white]  MATCHING BENCHMARK REPORT: {results['name']}{split_tag} [/bold white]")
    console.print(f"[bold cyan]================================================================[/bold cyan]\n")

    # Detailed Per-Category Precision / Recall table
    cat_table = Table(title="Per-Category Precision & Recall Breakdown", show_header=True, header_style="bold magenta")
    cat_table.add_column("Category", style="cyan")
    cat_table.add_column("Total", justify="right")
    cat_table.add_column("FP (Bad)", justify="right", style="red")
    cat_table.add_column("FN (Miss)", justify="right", style="yellow")
    cat_table.add_column("Precision", justify="right", style="bold green")
    cat_table.add_column("Recall", justify="right", style="bold green")
    cat_table.add_column("F1", justify="right", style="bold yellow")

    for cat, m in results["categories"].items():
        cat_table.add_row(
            cat,
            str(m["total"]),
            str(m["fp"]),
            str(m["fn"]),
            f"{m['precision']*100:.1f}%",
            f"{m['recall']*100:.1f}%",
            f"{m['f1']*100:.1f}%"
        )

    console.print(cat_table)

    # High-level Metrics summary table
    summary_table = Table(title=f"Overall Metrics ({results['split'].upper()} Split)", show_header=True, header_style="bold green")
    summary_table.add_column("Metric", style="bold")
    summary_table.add_column("Value", style="bold yellow")
    summary_table.add_column("Context / Operational Meaning", style="dim")

    summary_table.add_row("Total Pairs Evaluated", str(results["total"]), f"Evaluated under split '{results['split']}'")
    summary_table.add_row("Precision", f"{results['precision'] * 100:.1f}%", "Fraction of merged cards that are genuinely identical")
    summary_table.add_row("Recall", f"{results['recall'] * 100:.1f}%", "Fraction of identical items successfully merged")
    summary_table.add_row("F1 Score", f"{results['f1'] * 100:.1f}%", "Harmonic balance of precision and recall")
    summary_table.add_row("Accuracy", f"{results['accuracy'] * 100:.1f}%", "Overall correct decisions (True Merges + True Rejections)")
    summary_table.add_row("False Merges (FP)", f"[red]{results['fp']}[/red]", "HIGH RISK: Incompatible hardware grouped into same price comparison!")
    summary_table.add_row("Missed Merges (FN)", f"[yellow]{results['fn']}[/yellow]", "LOWER RISK: Item appears on separate card, missing price match")

    console.print(summary_table)

    # Detailed failures diagnostic
    if results["failures"]:
        console.print(f"\n[bold red]FAILURE DIAGNOSTICS ({len(results['failures'])} test cases failed):[/bold red]")
        for i, f in enumerate(results["failures"], 1):
            console.print(f"\n[bold yellow]#{i} [{f['id']} - {f['split'].upper()}] {f['error_type']} in '{f['category']}'[/bold yellow]")
            console.print(f"   [dim]Context: {f['description']}[/dim]")
            console.print(f"   Item A: [cyan]{f['title_a']}[/cyan]")
            console.print(f"   Item B: [cyan]{f['title_b']}[/cyan]")
            console.print(f"   Expected: [bold]{f['expected']}[/bold] | Actual: [bold red]{f['actual']}[/bold red] (Score: {f['score']})")
            console.print(f"   Matcher explanation: [italic]{f['reason']}[/italic]")
    else:
        console.print(f"\n[bold green]ALL {results['total']} TEST CASES IN '{results['split'].upper()}' PASSED![/bold green]")


def print_comparison_table(r1: Dict[str, any], r2: Dict[str, any]):
    comp_table = Table(title="A/B Benchmark Comparison: Baseline V1 vs Matcher V2", show_header=True, header_style="bold cyan")
    comp_table.add_column("Metric", style="bold")
    comp_table.add_column(r1["name"], style="yellow", justify="right")
    comp_table.add_column(r2["name"], style="bold green", justify="right")
    comp_table.add_column("Improvement / Delta", style="bold magenta", justify="right")

    p_delta = (r2['precision'] - r1['precision']) * 100
    r_delta = (r2['recall'] - r1['recall']) * 100
    f_delta = (r2['f1'] - r1['f1']) * 100
    a_delta = (r2['accuracy'] - r1['accuracy']) * 100
    fp_delta = r2['fp'] - r1['fp']
    fn_delta = r2['fn'] - r1['fn']

    comp_table.add_row("Precision", f"{r1['precision']*100:.1f}%", f"{r2['precision']*100:.1f}%", f"{p_delta:+.1f}%")
    comp_table.add_row("Recall", f"{r1['recall']*100:.1f}%", f"{r2['recall']*100:.1f}%", f"{r_delta:+.1f}%")
    comp_table.add_row("F1 Score", f"{r1['f1']*100:.1f}%", f"{r2['f1']*100:.1f}%", f"{f_delta:+.1f}%")
    comp_table.add_row("Accuracy", f"{r1['accuracy']*100:.1f}%", f"{r2['accuracy']*100:.1f}%", f"{a_delta:+.1f}%")
    comp_table.add_row("False Merges (FP)", str(r1['fp']), str(r2['fp']), f"{fp_delta:+d} (fewer bad merges)")
    comp_table.add_row("Missed Merges (FN)", str(r1['fn']), str(r2['fn']), f"{fn_delta:+d} (fewer split cards)")

    console.print("\n")
    console.print(comp_table)


if __name__ == "__main__":
    console.print("\n[bold]Running Benchmark on DEV Split (22 pairs)...[/bold]")
    dev_v1 = evaluate_matcher(baseline_matcher, matcher_name="Baseline V1", split="dev")
    dev_v2 = evaluate_matcher(match_components_v2, matcher_name="Matcher V2", split="dev")
    print_comparison_table(dev_v1, dev_v2)

    console.print("\n[bold]Running Benchmark on HELD-OUT Split (13 pairs)...[/bold]")
    held_v1 = evaluate_matcher(baseline_matcher, matcher_name="Baseline V1", split="held_out")
    held_v2 = evaluate_matcher(match_components_v2, matcher_name="Matcher V2", split="held_out")
    print_comparison_table(held_v1, held_v2)

    console.print("\n[bold]Full Production Benchmark (All 35 pairs):[/bold]")
    full_v2 = evaluate_matcher(match_components_v2, matcher_name="Matcher V2 (Production)", split=None)
    print_evaluation_report(full_v2)
