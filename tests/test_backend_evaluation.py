"""Verify the measuring instrument, including failure and regression behavior."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.evaluation.comparison import compare_reports, failure_keys
from src.evaluation.dataset import ROOT, load_dataset
from src.evaluation.metrics import (METRIC_VERSION, confusion_metrics, grouped_search,
                                    paired_bootstrap, ranking_metrics, summarize_search)


def query_case(identifier="q", returned=None, judgments=None, *, category="motor", successful=True):
    judgments = {"good": 3} if judgments is None else judgments
    returned = ["good"] if returned is None else returned
    return {"id": identifier, "kind": "search", "surface": "http", "category": category,
            "successful": successful, "metrics": ranking_metrics(returned, judgments),
            "checks": [{"name": "succeeded", "passed": successful, "critical": True}]}


def report(cases):
    return {"report_version": "1", "metric_version": METRIC_VERSION,
            "dataset_sha256": "fixed-dataset", "catalog_sha256": "fixed-catalog",
            "evaluator_sha256": "fixed-evaluator", "suite_config": {"refresh": False}, "cases": cases}


def test_confusion_counts_and_f1_are_hand_calculable():
    result = confusion_metrics([True, True, False, False], [True, False, True, False])
    assert {key: result[key] for key in ("tp", "fp", "tn", "fn")} == dict.fromkeys(("tp", "fp", "tn", "fn"), 1)
    assert result["precision"] == result["recall"] == result["f1"] == .5


def test_undefined_precision_is_not_reported_as_perfect():
    result = confusion_metrics([True, False], [False, False])
    assert result["precision"] is None
    assert result["recall"] == result["f1"] == 0
    empty = confusion_metrics([], [])
    assert empty["precision"] is empty["recall"] is empty["accuracy"] is None


def test_mismatched_prediction_denominators_are_rejected():
    with pytest.raises(ValueError):
        confusion_metrics([True], [])


def test_rank_metrics_use_fixed_cutoff_and_duplicates_earn_no_credit():
    result = ranking_metrics(["bad", "a", "a", "b"], {"a": 3, "b": 2})
    assert result["precision_at_5"] == .4
    assert result["returned_precision_at_5"] == .5
    assert result["recall_at_50"] == 1
    assert result["f1_at_50"] == pytest.approx(2 / 3)
    assert result["mrr"] == .5
    expected_dcg = 7 / math.log2(3) + 3 / math.log2(5)
    ideal_dcg = 7 + 3 / math.log2(3)
    assert result["ndcg_at_10"] == pytest.approx(expected_dcg / ideal_dcg)


def test_recall_cutoff_does_not_credit_a_hit_at_position_51():
    result = ranking_metrics([f"bad_{i}" for i in range(50)] + ["good"], {"good": 3})
    assert result["recall_at_50"] == result["f1_at_50"] == 0
    assert result["mrr"] == pytest.approx(1 / 51)


def test_empty_queries_have_separate_abstention_denominators():
    result = ranking_metrics([], {})
    assert result["recall_at_50"] is result["ndcg_at_10"] is None
    assert result["correct_abstention"] is True
    assert ranking_metrics(["wrong"], {})["correct_abstention"] is False


def test_crashed_request_is_not_successful_abstention():
    result = summarize_search([query_case(returned=[], judgments={}, successful=False)])
    assert result["correct_abstention_rate"] == 0
    assert result["error_rate"] == 1


def test_failed_positive_queries_stay_in_the_metric_denominator():
    result = summarize_search([query_case(), query_case("broken", returned=[], successful=False)])
    assert result["recall_at_50"] == .5
    assert result["false_empty_rate"] == .5
    assert result["error_rate"] == .5


def test_macro_average_does_not_give_more_weight_to_large_result_sets():
    large = {f"p{i}": 3 for i in range(10)}
    rows = [query_case(), query_case("large", returned=[], judgments=large, category="battery")]
    assert summarize_search(rows)["recall_at_50"] == .5
    assert grouped_search(rows, "category")["motor"]["recall_at_50"] == 1
    assert grouped_search(rows, "category")["battery"]["recall_at_50"] == 0


def test_same_scores_pass_and_repeatable_paired_intervals_are_zero():
    original = report([query_case()])
    result = compare_reports(copy.deepcopy(original), original)
    assert result["passed"] and not result["regressions"]
    assert result["paired_query_intervals"]["http/recall_at_50"]["ci95"] == [0, 0]


def test_improvement_elsewhere_cannot_hide_one_query_regression():
    old = report([query_case("one"), query_case("two", returned=[])])
    new = report([query_case("one", returned=[]), query_case("two")])
    result = compare_reports(new, old)
    assert not result["passed"]
    assert any(entry["case"] == "one" for entry in result["regressions"])
    assert any(entry["case"] == "two" for entry in result["improvements"])


def test_already_failing_check_detects_an_additional_bad_entity():
    old = report([query_case()])
    old["cases"][0]["checks"] = [{"name": "eligibility", "passed": False, "violations": ["bad1"]}]
    new = copy.deepcopy(old)
    new["cases"][0]["checks"][0]["violations"].append("bad2")
    result = compare_reports(new, old)
    assert not result["passed"]
    assert any(entry["case"].endswith("::bad2") for entry in result["regressions"])


def test_known_debt_is_visible_but_strict_mode_fails_it():
    original = report([query_case(successful=False, returned=[])])
    assert compare_reports(original, original)["passed"]
    assert not compare_reports(original, original, strict=True)["passed"]


@pytest.mark.parametrize("key", ["report_version", "metric_version", "dataset_sha256", "catalog_sha256", "evaluator_sha256", "suite_config"])
def test_incompatible_baselines_are_rejected(key):
    old = report([query_case()])
    new = copy.deepcopy(old)
    new[key] = "changed"
    with pytest.raises(ValueError, match=key):
        compare_reports(new, old)


def test_changed_case_population_is_not_silently_compared():
    with pytest.raises(ValueError, match="case identities"):
        compare_reports(report([query_case("other")]), report([query_case()]))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.1, 1.1])
def test_invalid_metric_values_cannot_silently_pass(value):
    original = report([query_case()])
    candidate = copy.deepcopy(original)
    candidate["cases"][0]["metrics"]["recall_at_50"] = value
    with pytest.raises(ValueError, match="Invalid metric"):
        compare_reports(candidate, original)


def test_already_failing_pagination_detects_further_coverage_loss():
    old = report([{"id":"pages", "kind":"pagination", "coverage":.8, "checks":[]}])
    new = copy.deepcopy(old)
    new["cases"][0]["coverage"] = .5
    assert not compare_reports(new, old)["passed"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -.1, 1.1, True])
def test_invalid_pagination_coverage_is_rejected(value):
    old = report([{"id": "pages", "kind": "pagination", "coverage": .8, "checks": []}])
    new = copy.deepcopy(old)
    new["cases"][0]["coverage"] = value
    with pytest.raises(ValueError, match="Invalid metric"):
        compare_reports(new, old)


def test_changed_case_kind_is_incompatible():
    old = report([query_case()])
    new = copy.deepcopy(old)
    new["cases"][0]["kind"] = "contract"
    with pytest.raises(ValueError, match="case definition"):
        compare_reports(new, old)


def test_failure_artifact_replaces_a_stale_passing_report(tmp_path):
    from src.evaluation.reporting import write_failure
    output = tmp_path / "result.json"
    output.write_text('{"passed":true}', encoding="utf-8")
    write_failure(output, "incompatible baseline")
    failure = json.loads(output.read_text(encoding="utf-8"))
    assert failure["evaluation_completed"] is False
    assert "passed" not in failure
    assert "failed" in output.with_suffix(".md").read_text(encoding="utf-8")


def test_matching_regression_is_detected_even_without_search_metrics():
    old = report([{"id": "pair", "kind": "matching", "correct": True, "checks": []}])
    new = copy.deepcopy(old)
    new["cases"][0]["correct"] = False
    assert not compare_reports(new, old)["passed"]


def test_paired_bootstrap_is_deterministic_and_reports_support():
    values = [.1, -.2, .3, 0]
    assert paired_bootstrap(values) == paired_bootstrap(values)
    assert paired_bootstrap(values)["support"] == 4
    assert paired_bootstrap([])["ci95"] is None


def test_dataset_has_independent_labels_and_deep_pagination_population():
    dataset = load_dataset()
    assert len(dataset["legacy_pairs"]) >= 45
    assert all(case.get("rationale") for case in dataset["searches"]["queries"])
    deep = next(case for case in dataset["searches"]["pagination"] if case["id"] == "http_deep_pages")
    assert len(deep["expected_entities"]) > 400
    assert len(deep["expected_entities"]) == len(set(deep["expected_entities"]))


def test_cli_never_overwrites_baseline_as_a_report(tmp_path):
    path = tmp_path / "baseline.json"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/evaluate_backend.py"),
                             "--write-baseline", str(path), "--reason", "test", "--output", str(path)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2
    assert not path.exists()


def test_missing_baseline_replaces_stale_report(tmp_path):
    output = tmp_path / "report.json"
    output.write_text('{"passed":true}', encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/evaluate_backend.py"),
                             "--baseline", str(tmp_path / "missing.json"), "--output", str(output)],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 2
    assert json.loads(output.read_text(encoding="utf-8"))["evaluation_completed"] is False
    assert "Baseline not found" in output.with_suffix(".md").read_text(encoding="utf-8")


@pytest.mark.parametrize("mutation", ["empty", "duplicate_surface", "duplicate_group", "duplicate_member", "catalog_collision"])
def test_invalid_dataset_cannot_reduce_or_duplicate_evaluation_cases(tmp_path, mutation):
    names = ("catalog.json", "search_cases.json", "matching_scenarios.json")
    catalog, searches, matching = [json.loads((ROOT / "evaluations" / name).read_text(encoding="utf-8")) for name in names]
    if mutation == "empty":
        searches["queries"] = []
    elif mutation == "duplicate_surface":
        searches["queries"][0]["surfaces"] = ["http", "http"]
    elif mutation == "duplicate_group":
        matching["groups"].append(copy.deepcopy(matching["groups"][0]))
    elif mutation == "duplicate_member":
        matching["groups"][0]["members"].append(matching["groups"][0]["members"][0])
    else:
        catalog["products"][0]["key"] = f"{catalog['pagination_support']['prefix']}_000"
    for name, contents in zip(names, (catalog, searches, matching)):
        (tmp_path / name).write_text(json.dumps(contents), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(tmp_path)


@pytest.fixture(scope="module")
def completed_evaluation(tmp_path_factory):
    directory = tmp_path_factory.mktemp("backend_quality")
    output, baseline = directory / "current.json", directory / "baseline.json"
    # Guard both the production DB and real network access in the child process.
    # Fixture ASGI requests and MCP tool dispatch do not need sockets.
    guard = '''
import pathlib, runpy, socket, sqlite3, sys
original_connect = sqlite3.connect
production = pathlib.Path('data/components.db').resolve()
def connect(path, *args, **kwargs):
    if str(production).replace('\\\\','/').lower() in str(path).replace('\\\\','/').lower():
        raise AssertionError('Evaluation attempted to open the production catalog')
    return original_connect(path, *args, **kwargs)
sqlite3.connect = connect
real_socket_connect = socket.socket.connect
def guarded_socket_connect(sock, address, *args, **kwargs):
    # Windows asyncio creates a loopback socket pair for its internal wakeup.
    if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return real_socket_connect(sock, address, *args, **kwargs)
    raise AssertionError('Evaluation attempted a network connection')
socket.socket.connect = guarded_socket_connect
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    args = [sys.executable, "-c", guard, str(ROOT / "scripts/evaluate_backend.py"),
            "--write-baseline", str(baseline), "--reason", "Automated evaluator verification", "--output", str(output)]
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(output.read_text(encoding="utf-8")), baseline, directory


def test_complete_pipeline_reports_known_defects_without_certifying_them(completed_evaluation):
    result, _, _ = completed_evaluation
    cases = {case["id"]: case for case in result["cases"]}
    assert len(cases) == result["summary"]["case_count"] >= 180
    assert "contract/http_refresh_filter" in cases
    assert "pagination/http_deep_pages" in cases
    assert "matching/hybrid/adversarial/saved_label_pin_conflict" in cases
    assert "grouping/resistor_packages" in cases
    assert result["summary"]["search_by_surface"]["mcp"]["support"] > 20
    assert result["scope"].startswith("Controlled fixture")
    assert "comparison" not in result


def test_repeat_run_matches_baseline_and_strict_mode_exposes_debt(completed_evaluation):
    original, baseline, directory = completed_evaluation
    output = directory / "repeat.json"
    command = [sys.executable, str(ROOT / "scripts/evaluate_backend.py"), "--baseline", str(baseline), "--output", str(output)]
    environment = {**os.environ, "PRICE_STOCK_TTL": "999999999999", "PRICE_STOCK_TIMEOUT": "0",
                   "MATCHER_MODE": "v2_only", "PRICE_STOCK_REFRESH": "true"}
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    repeated = json.loads(output.read_text(encoding="utf-8"))
    assert repeated["summary"] == original["summary"]
    assert repeated["comparison"]["passed"]
    assert failure_keys(repeated) == failure_keys(original)
    assert compare_reports(repeated, original, strict=True)["passed"] == original["summary"]["absolute_passed"]


def test_real_matcher_regression_fails_the_cli_gate(completed_evaluation):
    _, baseline, directory = completed_evaluation
    output = directory / "mutated.json"
    # Deliberately break the actual engine in an isolated child process. This
    # proves the complete measuring/gating path catches a backend behavior change,
    # rather than only testing comparison of hand-authored report dictionaries.
    mutation = '''
import runpy, sys
from src.evaluation.runner import Runtime
original_init = Runtime.__init__
def broken_init(self, *args, **kwargs):
    original_init(self, *args, **kwargs)
    for engine in self.engines:
        engine._evaluate_pair_merge = lambda *args, **kwargs: (False, 'injected regression', 'test')
Runtime.__init__ = broken_init
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    result = subprocess.run([sys.executable, "-c", mutation, str(ROOT / "scripts/evaluate_backend.py"),
                             "--baseline", str(baseline), "--output", str(output)],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 1, result.stdout + result.stderr
    comparison = json.loads(output.read_text(encoding="utf-8"))["comparison"]
    assert not comparison["passed"]
    assert any(entry["kind"] == "matching" and entry["case"].startswith("matching/hybrid/")
               for entry in comparison["regressions"])
