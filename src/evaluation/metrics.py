"""Pure quality measures, independent of the search implementation.

Judgments are exhaustive within the versioned evaluation catalog. Grade >= 2 is
relevant; grades 0..3 supply graded nDCG gains. P@k uses a fixed k denominator.
Queries with no relevant answers have separate abstention metrics, not invented
recall/nDCG values. Undefined classifier precision is None, never a perfect 1.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

METRIC_VERSION = "1.0.0"
RANKING_METRICS = (
    "precision_at_5", "returned_precision_at_5", "recall_at_50",
    "returned_precision_at_50", "f1_at_50", "ndcg_at_10", "mrr",
)


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def confusion_metrics(truth: Sequence[bool], predictions: Sequence[bool]) -> dict[str, Any]:
    if len(truth) != len(predictions):
        raise ValueError("Truth and predictions must have the same length.")
    tp = sum(t and p for t, p in zip(truth, predictions))
    fp = sum(not t and p for t, p in zip(truth, predictions))
    fn = sum(t and not p for t, p in zip(truth, predictions))
    tn = sum(not t and not p for t, p in zip(truth, predictions))
    return {
        "support": len(truth), "positives": tp + fn, "predicted_positives": tp + fp,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
        "f1": ratio(2 * tp, 2 * tp + fp + fn),
        "accuracy": ratio(tp + tn, len(truth)),
    }


def ranking_metrics(returned: Sequence[str], judgments: Mapping[str, int]) -> dict[str, Any]:
    # Duplicates consume ranking positions but can never earn relevance twice.
    seen: set[str] = set()
    gains, relevant = [], []
    for identifier in returned:
        grade = judgments.get(identifier, 0) if identifier not in seen else 0
        seen.add(identifier)
        gains.append(grade)
        relevant.append(grade >= 2)
    positives = sum(grade >= 2 for grade in judgments.values())
    result: dict[str, Any] = {"relevant_support": positives, "returned": len(returned)}
    if not positives:
        return {**result, **dict.fromkeys(RANKING_METRICS),
                "correct_abstention": not returned, "false_empty": None}
    hits = sum(relevant[:50])
    precision = hits / min(50, len(returned)) if returned else 0.0
    recall = hits / positives
    dcg = sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(gains[:10]))
    ideal = sorted(judgments.values(), reverse=True)[:10]
    idcg = sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(ideal))
    return {
        **result, "precision_at_5": sum(relevant[:5]) / 5,
        "returned_precision_at_5": sum(relevant[:5]) / min(5, len(returned)) if returned else 0.0,
        "returned_precision_at_50": precision, "recall_at_50": recall,
        "f1_at_50": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "ndcg_at_10": dcg / idcg,
        "mrr": next((1 / (i + 1) for i, hit in enumerate(relevant) if hit), 0.0),
        "correct_abstention": None, "false_empty": not returned,
    }


def mean_defined(values: Iterable[float | None]) -> float | None:
    defined = [value for value in values if value is not None]
    return statistics.mean(defined) if defined else None


def summarize_search(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Equal weight per query; all errored positive queries retain zero scores."""
    return {
        "support": len(cases),
        "answerable_queries": sum(c["metrics"]["relevant_support"] > 0 for c in cases),
        "empty_answer_queries": sum(c["metrics"]["relevant_support"] == 0 for c in cases),
        **{key: mean_defined(c["metrics"][key] for c in cases) for key in RANKING_METRICS},
        "false_empty_rate": mean_defined(c["metrics"]["false_empty"] for c in cases),
        # A crashed request is never a successful abstention.
        "correct_abstention_rate": mean_defined(
            (float(c["metrics"]["correct_abstention"]) if c["successful"] else 0.0)
            if c["metrics"]["correct_abstention"] is not None else None for c in cases
        ),
        "error_rate": mean_defined(not c["successful"] for c in cases),
    }


def grouped_search(cases: Sequence[dict[str, Any]], field: str) -> dict[str, Any]:
    groups: dict[str, list] = defaultdict(list)
    for case in cases:
        groups[case[field]].append(case)
    return {name: summarize_search(rows) for name, rows in sorted(groups.items())}


def paired_bootstrap(deltas: Sequence[float], samples: int = 2000, seed: int = 1729) -> dict[str, Any]:
    """Paired query bootstrap; descriptive interval, never a replacement for gates."""
    if not deltas:
        return {"support": 0, "mean_delta": None, "ci95": None}
    rng = random.Random(seed)
    means = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(samples))
    return {"support": len(deltas), "mean_delta": statistics.mean(deltas),
            "ci95": [means[int(samples * .025)], means[min(samples - 1, int(samples * .975))]]}
