"""
Measure hybrid specification retrieval before/after the vendor layer.

The suite is intentionally small and engineering-oriented. It reports candidate
counts by tier, recall-gap diagnostics, and p50/p95 latency. It is not a
ground-truth recall benchmark: it measures how much of the catalog the filter
surface can see and whether the mandatory constraints are respected.

Usage:
    python scripts/benchmark_spec_retrieval.py
    python scripts/benchmark_spec_retrieval.py --json reports/spec_benchmark.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from mcp_server.server import count_components, search_components

CASES: List[Dict[str, Any]] = [
    {
        "name": "motor 12V",
        "category": "motor",
        "filters": {"voltage": "12"},
    },
    {
        "name": "motor 12V + rpm <= 1000",
        "category": "motor",
        "filters": {"voltage": "12", "rpm": {"lte": 1000}},
    },
    {
        "name": "battery 3.7V >= 1500mAh",
        "category": "battery",
        "filters": {"voltage": "3.7", "capacity_mah": {"gte": 1500}},
    },
    {
        "name": "battery >= 20A discharge",
        "category": "battery",
        "filters": {"max_discharge_current_a": {"gte": 20}},
    },
    {
        "name": "microcontroller pins >= 8, 3.3V",
        "category": "microcontrollers",
        "filters": {"pins": {"gte": 8}, "supply_voltage_v": {"lte": 3.6}},
    },
    {
        "name": "switch 5V, >= 1A",
        "category": "switches",
        "filters": {"voltage_v": "5", "contact_current_a": {"gte": 1}},
    },
    {
        "name": "manufacturer = 3peak",
        "filters": {"manufacturer": "3peak"},
    },
    {
        "name": "operating temperature >= 85C",
        "filters": {"operating_temperature": {"gte": 85}},
    },
]


def _duration(fn, repeats: int = 3) -> float:
    samples = []
    for _ in range(max(1, repeats)):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def _constraint_errors(rows: List[Dict[str, Any]], filters: Dict[str, Any]) -> int:
    errors = 0
    for row in rows:
        specs = row.get("specs") or {}
        for key, expected in filters.items():
            if key not in specs:
                errors += 1
                continue
            try:
                value = float(specs[key])
            except (TypeError, ValueError):
                continue
            if isinstance(expected, dict):
                if "gte" in expected and value < float(expected["gte"]):
                    errors += 1
                if "lte" in expected and value > float(expected["lte"]):
                    errors += 1
                if "eq" in expected and abs(value - float(expected["eq"])) > 1e-9:
                    errors += 1
            else:
                try:
                    if abs(value - float(expected)) > 1e-9:
                        errors += 1
                except (TypeError, ValueError):
                    pass
    return errors


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="", help="write results as JSON")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)

    results = []
    for case in CASES:
        kwargs = {
            "category": case.get("category"),
            "filters": case.get("filters"),
            "include_vendor_specs": True,
        }
        hybrid = count_components(**kwargs)
        promoted = count_components(
            category=case.get("category"),
            filters=case.get("filters"),
            include_vendor_specs=False,
        )
        recall = (hybrid.data or {}).get("filter_recall") or {}
        search = search_components(
            category=case.get("category"),
            filters=case.get("filters"),
            include_vendor_specs=True,
            limit=20,
            refresh=False,
        )
        rows = (search.data or {}).get("results") or []
        result = {
            "case": case["name"],
            "promoted_matches": promoted.total_matches if promoted.ok else 0,
            "hybrid_matches": hybrid.total_matches,
            "vendor_fallback_matches": recall.get("vendor_fallback_matches", 0),
            "missing_spec": recall.get("missing_spec", 0),
            "promoted_missing": recall.get("promoted_missing", 0),
            "mode": recall.get("mode"),
            "constraint_errors_in_page": _constraint_errors(rows, case.get("filters") or {}),
            "count_p50_ms": round(_duration(lambda: count_components(**kwargs), args.repeats), 2),
            "search_p50_ms": round(_duration(
                lambda: search_components(
                    category=case.get("category"),
                    filters=case.get("filters"),
                    include_vendor_specs=True,
                    limit=20,
                    refresh=False,
                ),
                args.repeats,
            ), 2),
        }
        results.append(result)
        print(
            f"{result['case']:<36} promoted={result['promoted_matches']:<6} "
            f"hybrid={result['hybrid_matches']:<6} fallback={result['vendor_fallback_matches']:<6} "
            f"missing={result['missing_spec']:<6} errors={result['constraint_errors_in_page']:<3} "
            f"count={result['count_p50_ms']}ms search={result['search_p50_ms']}ms"
        )

    if args.json:
        path = Path(args.json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
