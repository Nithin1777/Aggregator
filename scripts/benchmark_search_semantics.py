"""Repeated cached-tool latency and concurrency checks; no vendor requests.

Usage: python scripts/benchmark_search_semantics.py --repeats 5 --workers 4 --output scratch/semantic_benchmark.json
The results measure this catalog and machine, not ground-truth precision/recall.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcp_server.server import count_components, search_components

CASES = {
    "motor_torque": {"category": "motor", "filters": {"torque_kgcm": {"gte": 40}}},
    "motor_speed": {"category": "motor", "filters": {"rpm": {"gte": 50, "lte": 150}}},
    "battery_compound": {"category": "battery", "filters": {"capacity_mah": {"gte": 10000}, "continuous_discharge_current_a": {"gte": 20}}},
    "vendor_mounting": {"category": "switches", "filters": {"mounting_type": "surface mount"}},
    "mcu_discovery": {"category": "microcontrollers", "query": "ESP32"},
    "runtime_requirement": {"query": "battery to power an arduino for 8 hours"},
}


def run_case(item):
    name, params = item
    measurements = {"case": name}
    start = time.perf_counter()
    count = count_components(**params)
    measurements["count_ms"] = (time.perf_counter() - start) * 1000
    start = time.perf_counter()
    search = search_components(**params, limit=5, refresh=False, include_partial_candidates=True)
    measurements["search_ms"] = (time.perf_counter() - start) * 1000
    assert count.ok and search.ok, (name, count.message, search.message)
    assert count.total_matches == search.total_matches, name
    for response in (count, search):
        recall = response.data.get("filter_recall")
        if recall:
            assert recall["base_matches"] == sum(recall[k] for k in ("confirmed_matches", "filtered_out_for_value", "missing_spec")), name
            assert recall["confirmed_matches"] == response.total_matches, name
    for candidate in search.data.get("partial_candidates", []):
        assert candidate["satisfied"] and candidate["unknown"] and not candidate["failed"], name
        assert candidate["product_id"] not in {row["product_id"] for row in search.data["results"]}, name
    measurements["total_matches"] = search.total_matches
    measurements["partial_candidates"] = len(search.data.get("partial_candidates", []))
    return measurements


def summarize(samples):
    report = {}
    for name in CASES:
        subset = [s for s in samples if s["case"] == name]
        row = {"samples": len(subset), "total_matches": subset[-1]["total_matches"],
               "partial_candidates": subset[-1]["partial_candidates"]}
        for operation in ("count", "search"):
            values = sorted(s[f"{operation}_ms"] for s in subset)
            row[operation] = {"p50_ms": round(statistics.median(values), 2),
                              "p95_ms": round(values[math.ceil(0.95 * len(values)) - 1], 2)}
        report[name] = row
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or args.workers < 1:
        parser.error("repeats and workers must be positive")
    run_case(next(iter(CASES.items())))  # warm the shared policy and database caches
    jobs = list(CASES.items()) * args.repeats
    sequential = [run_case(item) for item in jobs]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        concurrent = list(pool.map(run_case, jobs))
    report = {"refresh": False, "workers": args.workers, "repeats": args.repeats,
              "p95_method": "nearest rank; small-sample diagnostic, not a service-level estimate",
              "sequential": summarize(sequential), "concurrent": summarize(concurrent)}
    payload = json.dumps(report, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()
