"""Execute frozen judgments through actual backend entry points, without vendors.

Run in a dedicated process (scripts/evaluate_backend.py). Settings are redirected
before importing either server: both currently initialize SQLite during import.
No application/query/identity/filter functions are patched by this evaluator.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import gc
from importlib.metadata import version
import itertools
import json
import logging
import math
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time

from .dataset import ROOT, digest, load_dataset, restore_catalog, seed_catalog
from .metrics import METRIC_VERSION, confusion_metrics, grouped_search, summarize_search

REPORT_VERSION = "1.0.0"
SUITE_CONFIG = {"refresh": "fake_vendors_only", "ranking_unit": "distinct_product_entity",
                "relevant_grade_min": 2, "requested_page_size": 50, "ranking_depth": 50,
                "aggregation": "macro_query", "matching_modes": ["v2", "hybrid"],
                "refresh_environment": "source_defaults",
                "fixture_isolation": "restore_before_every_case"}


def check(name, passed, *, critical=True, details=None, violations=None):
    return {"name": name, "passed": bool(passed), "critical": critical,
            "details": details, "violations": sorted(violations or [])}


def mcp_payload(result):
    """FastMCP can return content blocks or (content, structured_output)."""
    if isinstance(result, tuple):
        blocks, structured = result
        if isinstance(structured, dict):
            return structured
    else:
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
        blocks = getattr(result, "content", result)
    texts = [block.text for block in blocks if getattr(block, "type", None) == "text"]
    if len(texts) != 1:
        raise ValueError("Expected exactly one MCP JSON content block.")
    payload = json.loads(texts[0])
    if not isinstance(payload, dict):
        raise ValueError("Expected an MCP object envelope.")
    return payload


class FixtureAdapter:
    def __init__(self, vendor, runtime):
        self.vendor, self.runtime = vendor, runtime

    async def search_live(self, query, limit=20):
        from models import UnifiedProduct
        result = []
        for pid, original in self.runtime.products.items():
            if original["vendor"] != self.vendor or original["key"].startswith("metadata_"):
                continue
            product = {**original, **self.runtime.live_overrides.get(original["key"], {})}
            result.append(UnifiedProduct(
                id=pid, vendor=self.vendor, vendor_name=self.vendor, external_id=product["key"],
                title=product["title"], normalized_title=product["title"].lower(), mpn=product.get("mpn"),
                sku=product["key"], price=product["price"], available=product["available"],
                category=product["category"], category_canonical=product["category"],
                product_url=f"https://fixture.invalid/{product['key']}",
            ))
        return result[:limit]

    async def close(self):
        pass


class Runtime:
    def __init__(self, directory, dataset):
        self.directory, self.dataset = directory, dataset
        self.template = directory / "template.db"
        self.working = directory / "working.db"
        from config import settings
        settings.db_path = self.template
        settings.matcher_audit_logging = False
        settings.matcher_audit_log_path = directory / "audit.jsonl"
        settings.price_stock_refresh_enabled = False
        settings.matcher_mode = "hybrid"
        # Environment tuning must not change the measuring conditions. Read
        # defaults from this revision so changes to backend defaults remain visible.
        for field in ("price_stock_ttl_seconds", "price_stock_refresh_timeout_seconds"):
            setattr(settings, field, type(settings).model_fields[field].default)
        self.settings = settings
        self.products = seed_catalog(self.template, dataset["catalog"])
        self.by_key = {p["key"]: (pid, p) for pid, p in self.products.items()}
        restore_catalog(self.template, self.working)
        settings.db_path = self.working
        # These imports must follow redirection above.
        import db
        import main
        from mcp_server import server
        self.db, self.main, self.server = db, main, server
        self.engines = [main.get_engine(), server._engine]
        self.live_overrides = {}
        vendors = {p["vendor"] for p in self.products.values()}
        for engine in self.engines:
            engine.adapters = {vendor: FixtureAdapter(vendor, self) for vendor in vendors}
            engine._llm_labels = {}

    def reset(self, before=None, live=None):
        restore_catalog(self.template, self.working)
        self.db.invalidate_counts_cache()
        self.settings.price_stock_refresh_enabled = False
        self.live_overrides = live or {}
        self.server._description_schema_ready = False
        for engine in self.engines:
            engine._llm_labels = {}
            engine._refresh_in_progress = False
            engine._refresh_cooldown_until = 0
        if before:
            conn = self.db.get_db_connection()
            try:
                with conn:
                    for key, values in before.items():
                        pid, original = self.by_key[key]
                        conn.execute("UPDATE products SET price=?,available=? WHERE id=?",
                                     (values.get("price", original["price"]), int(values.get("available", original["available"])), pid))
            finally:
                conn.close()

    async def close(self):
        for engine in self.engines:
            tasks = list(engine._refresh_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await engine.close()
        if self.server._refresh_loop:
            # The bridge loop owns only fake adapters in this process.
            self.server._refresh_loop.call_soon_threadsafe(self.server._refresh_loop.stop)
            self.server._refresh_loop_thread.join(timeout=5)
            self.server._refresh_loop.close()

    def identity(self, row):
        pid = row.get("product_id", row.get("id"))
        if pid in self.products:
            return self.products[pid]["key"]
        key = row.get("external_id") or str(row.get("product_url", "")).rsplit("/", 1)[-1]
        return key if key in self.by_key else f"UNKNOWN:{pid or key}"

    def entity(self, key):
        return self.by_key[key][1]["entity"] if key in self.by_key else key

    def normalize(self, surface, payload):
        if surface == "mcp":
            body = payload.get("data") or {}
            return [self.identity(row) for row in body.get("results", [])], body, []
        groups = payload.get("comparison_groups", [])
        # The user sees ranked comparison cards; use their offer order instead
        # of the separate results array, which retains candidate retrieval order.
        listings = [self.identity(row) for group in groups for row in group.get("offers", [])]
        if not groups:
            listings = [self.identity(row) for row in payload.get("results", [])]
        return listings, payload, groups


def requests_for(case, surface, *, limit=50):
    if surface == "http":
        request = {"q": case.get("query", ""), "source": "local", "refresh": "off", "page_size": limit}
        mapping = {"category": "category", "in_stock": "in_stock", "price_max": "price_max", "vendor": "vendor"}
        if case.get("filters"):
            request["spec_filters"] = json.dumps(case["filters"])
    else:
        request = {"limit": limit, "refresh": False}
        if "query" in case:
            request["query"] = case["query"]
        mapping = {"category": "category", "in_stock": "in_stock_only", "price_max": "price_max", "filters": "filters"}
    request.update({target: case[key] for key, target in mapping.items() if key in case})
    return request


def response_shape(surface, payload):
    if surface == "http":
        return (isinstance(payload.get("query"), str) and type(payload.get("total_results")) is int
                and isinstance(payload.get("results"), list) and isinstance(payload.get("comparison_groups"), list))
    body = payload.get("data")
    return (payload.get("ok") is True and isinstance(body, dict) and isinstance(body.get("results"), list)
            and type(payload.get("total_matches")) is int and type(payload.get("returned")) is int)


def wire_constraints(runtime, surface, payload, case):
    """Check serialized price/stock/vendor values against independently stated bounds."""
    rows = (payload.get("data") or {}).get("results", []) if surface == "mcp" else [
        offer for group in payload.get("comparison_groups", []) for offer in group.get("offers", [])]
    if surface == "http" and not rows:
        rows = payload.get("results", [])
    violations = []
    ceiling = case.get("price_max", case.get("wire_price_max"))
    for row in rows:
        key = runtime.identity(row)
        try:
            price = float(row["price"])
            if not math.isfinite(price) or price < 0 or (ceiling is not None and price > ceiling):
                violations.append(f"price:{key}")
        except (KeyError, TypeError, ValueError):
            violations.append(f"price:{key}")
        if case.get("in_stock") and not (row.get("available") is True or row.get("stock_status") == "in_stock"):
            violations.append(f"stock:{key}")
        if case.get("vendor") and row.get("vendor") != case["vendor"]:
            violations.append(f"vendor:{key}")
    return violations


async def dispatch(runtime, client, surface, request, tool="search_components"):
    started = time.perf_counter()
    if surface == "http":
        response = await client.get("/api/search", params=request)
        try:
            payload = response.json()
        except ValueError:
            payload = {"transport_error": response.text[:300]}
        status = response.status_code
        successful = status == 200
    else:
        payload = mcp_payload(await runtime.server.mcp.call_tool(tool, request))
        status, successful = 200, payload.get("ok") is True
    return payload, status, successful, (time.perf_counter() - started) * 1000


async def evaluate_query(runtime, client, case, surface):
    from .metrics import ranking_metrics
    runtime.reset()
    request = requests_for(case, surface)
    record = {"id": f"search/{surface}/{case['id']}", "kind": "search", "surface": surface,
              "category": case["category"], "request": request, "judgments": case["relevance"],
              "rationale": case["rationale"], "checks": []}
    try:
        payload, status, successful, duration = await dispatch(runtime, client, surface, request)
        listings, body, groups = runtime.normalize(surface, payload)
        entities = list(dict.fromkeys(runtime.entity(key) for key in listings)) if successful else []
        unknown = [key for key in listings if key not in runtime.by_key]
        irrelevant = sorted(entity for entity in entities if case["relevance"].get(entity, 0) < 2)
        missing = sorted(entity for entity, grade in case["relevance"].items() if grade >= 2 and entity not in entities)
        checks = [check("request_succeeded", successful, details={"status": status, "error": payload.get("error_code")}),
                  check("response_schema", response_shape(surface, payload)),
                  check("known_listing_ids", not unknown, violations=unknown),
                  check("eligible_entities", not irrelevant, violations=irrelevant),
                  check("all_relevant_found", not missing, critical=False, violations=missing)]
        overmerges, incompatible_groups = [], 0
        for group in groups:
            members = sorted({runtime.entity(runtime.identity(offer)) for offer in group.get("offers", [])})
            overmerges.extend("|".join(pair) for pair in itertools.combinations(members, 2))
            incompatible_groups += len(members) > 1
        checks.append(check("comparison_groups_compatible", not overmerges, violations=overmerges))
        wire_violations = wire_constraints(runtime, surface, payload, case)
        checks.append(check("serialized_constraints", not wire_violations, violations=wire_violations))
        if case.get("allowed_listings") is not None:
            bad_offers = sorted(set(listings) - set(case["allowed_listings"]))
            checks.append(check("eligible_listing_offers", not bad_offers, violations=bad_offers))
        if case.get("require_unresolved"):
            checks.append(check("requirements_not_claimed_verified", body.get("requirements_verified") is False and bool(body.get("unresolved_constraints"))))
        if surface == "mcp":
            count_request = {key: value for key, value in request.items() if key not in {"refresh", "limit"}}
            count, _, count_ok, _ = await dispatch(runtime, client, surface, count_request, tool="count_components")
            checks.append(check("count_search_agree", count_ok and count.get("total_matches") == payload.get("total_matches"),
                                details={"count": count.get("total_matches"), "search": payload.get("total_matches")}))
        record.update(successful=successful, returned_entities=entities, returned_listings=listings,
                      group_count=len(groups), incompatible_group_count=incompatible_groups,
                      latency_ms=duration, checks=checks, metrics=ranking_metrics(entities[:50], case["relevance"]))
    except Exception as exc:
        record.update(successful=False, returned_entities=[], returned_listings=[], latency_ms=None,
                      error=f"{type(exc).__name__}: {exc}"[:500],
                      checks=[check("request_succeeded", False)], metrics=ranking_metrics([], case["relevance"]))
    return record


async def evaluate_contract(runtime, client, case):
    runtime.reset(case.get("before"), case.get("live"))
    if "live" in case:
        runtime.settings.price_stock_refresh_enabled = True
    surface = case["surface"]
    request = ({"refresh": "off", "page_size": 50} if surface == "http" else {"refresh": False}) | case["request"]
    record = {"id": f"contract/{case['id']}", "kind": "contract", "surface": surface,
              "category": "contracts", "request": request, "rationale": case["rationale"]}
    try:
        payload, status, successful, duration = await dispatch(runtime, client, surface, request)
        if "expected_status" in case:
            checks = [check("expected_client_error", status in case["expected_status"], details={"status": status})]
        elif "expected_error" in case:
            checks = [check("expected_domain_error", payload.get("ok") is False and payload.get("error_code") == case["expected_error"],
                            details={"error_code": payload.get("error_code")})]
        elif case.get("require_unresolved"):
            _, body, _ = runtime.normalize(surface, payload)
            checks = [check("request_succeeded", successful),
                      check("requirements_not_claimed_verified", body.get("requirements_verified") is False and bool(body.get("unresolved_constraints")))]
        else:
            listings, _, _ = runtime.normalize(surface, payload)
            entities = sorted({runtime.entity(key) for key in listings})
            checks = [check("request_succeeded", successful, details={"status": status}),
                      check("eligible_entities", entities == sorted(case["expected_entities"]),
                            details={"actual": entities, "expected": case["expected_entities"]},
                            violations=sorted(set(entities) ^ set(case["expected_entities"])))]
        record.update(checks=checks, latency_ms=duration)
    except Exception as exc:
        record.update(checks=[check("request_succeeded", False)], error=f"{type(exc).__name__}: {exc}"[:500], latency_ms=None)
    return record


async def evaluate_pagination(runtime, client, case):
    runtime.reset()
    surface = case["surface"]
    seen_listings, seen_entities, cursors, checks = [], [], set(), []
    request = requests_for(case, surface, limit=case["page_size"])
    terminated, pages, coverage = False, 0, 0.0
    try:
        for step in range(case["max_pages"]):
            page = case.get("start_page", 1) + step
            pages = step + 1
            if surface == "http":
                request["page"] = page
            payload, _, successful, _ = await dispatch(runtime, client, surface, request)
            if not successful:
                raise ValueError(f"Pagination request failed: {payload.get('error_code', 'HTTP error')}")
            listings, _, _ = runtime.normalize(surface, payload)
            seen_listings.extend(listings)
            seen_entities.extend(runtime.entity(key) for key in listings)
            cursor = payload.get("next_cursor")
            more = payload.get("has_next") if surface == "http" else bool(cursor)
            if not more:
                terminated = True
                break
            if surface == "mcp":
                if cursor in cursors:
                    raise ValueError("Pagination cursor repeated.")
                cursors.add(cursor)
                request["cursor"] = cursor
        duplicates = sorted(key for key in set(seen_listings) if seen_listings.count(key) > 1)
        expected, observed = set(case["expected_entities"]), set(seen_entities)
        coverage = min(1.0, len(observed & expected) / case.get("expected_returned_count", len(expected))) if expected else float(not observed)
        checks = [check("pagination_terminates", terminated),
                  check("no_duplicate_listings", not duplicates, violations=duplicates),
                  check("eligible_entities", observed <= expected, violations=sorted(observed - expected))]
        if "expected_returned_count" in case:
            checks.append(check("deep_boundary_population", len(seen_listings) == case["expected_returned_count"],
                                details={"expected": case["expected_returned_count"], "actual": len(seen_listings), "start_page": case["start_page"]}))
        else:
            checks.append(check("pagination_complete", observed == expected, violations=sorted(observed ^ expected)))
    except Exception as exc:
        checks = [check("pagination_succeeded", False, details=f"{type(exc).__name__}: {exc}"[:500])]
    return {"id": f"pagination/{case['id']}", "kind": "pagination", "surface": surface,
            "category": case["category"], "checks": checks, "pages": pages,
            "strategy": "deep_boundary_probe" if "expected_returned_count" in case else "exhaustive_walk",
            "coverage": coverage,
            "returned_entities": list(dict.fromkeys(seen_entities)), "returned_listings": seen_listings}


def evaluate_matching(runtime):
    from normalizer import ComponentItem, match_components_v2
    engine = runtime.engines[0]
    result = []
    reviewed_pairs = [{**pair, "category": pair.get("category", "production_verified_stress"),
                       "split": pair.get("split", "production_verified_stress"), "should_match": pair["label"] == "same",
                       "expected_label": pair["label"]}
                      for pair in runtime.dataset["reviewed_matching_pairs"]]
    for pair in runtime.dataset["legacy_pairs"] + runtime.dataset["matching"]["pairs"] + reviewed_pairs:
        for mode in ("v2", "hybrid"):
            runtime.reset()
            split = pair.get("split", "adversarial")
            record = {"id": f"matching/{mode}/{split}/{pair['id']}", "kind": "matching", "mode": mode,
                      "surface": "matcher", "category": pair["category"], "split": split,
                      "expected": pair["should_match"], "expected_label": pair.get("expected_label"),
                      "rationale": pair.get("reason"), "label_confidence": pair.get("confidence"),
                      "title_a": pair["title_a"], "title_b": pair["title_b"]}
            try:
                if mode == "v2":
                    predicted, _, reason = match_components_v2(pair["title_a"], pair["title_b"])
                else:
                    if pair.get("saved_label"):
                        pair_hash = hashlib.sha1("::".join(sorted([pair["title_a"].lower().strip(), pair["title_b"].lower().strip()])).encode()).hexdigest()[:16]
                        engine._llm_labels = {pair_hash: {"label": pair["saved_label"], "confidence": "high", "reason": "adversarial fixture input"}}
                    predicted, reason, _ = engine._evaluate_pair_merge(ComponentItem(pair["title_a"]), ComponentItem(pair["title_b"]))
                correct = bool(predicted) == pair["should_match"]
                record.update(predicted=bool(predicted), correct=correct, reason=reason,
                              checks=[check("matching_judgment", correct, critical=not pair["should_match"])])
            except Exception as exc:
                record.update(predicted=None, correct=False, error=f"{type(exc).__name__}: {exc}"[:500],
                              checks=[check("matching_succeeded", False)])
            result.append(record)
    return result


def evaluate_groups(runtime):
    result = []
    for case in runtime.dataset["matching"]["groups"]:
        runtime.reset()
        ids = [runtime.by_key[key][0] for key in case["members"]]
        record = {"id": f"grouping/{case['id']}", "kind": "grouping", "surface": "grouping", "category": "compatibility"}
        try:
            groups = runtime.engines[0].group_and_compare(runtime.db.get_products_by_ids(ids))
            clusters = [[runtime.identity(offer.model_dump()) for offer in group.offers] for group in groups]
            membership = {key: index for index, group in enumerate(clusters) for key in group}
            truth, predictions, bad, missed = [], [], [], []
            for a, b in itertools.combinations(case["members"], 2):
                expected = runtime.entity(a) == runtime.entity(b)
                actual = a in membership and b in membership and membership[a] == membership[b]
                truth.append(expected)
                predictions.append(actual)
                if actual and not expected:
                    bad.append("|".join(sorted((a, b))))
                if expected and not actual:
                    missed.append("|".join(sorted((a, b))))
            dropped = sorted(set(case["members"]) - membership.keys())
            checks = [check("no_incompatible_merges", not bad, violations=bad),
                      check("equivalent_offers_grouped", not missed, critical=False, violations=missed),
                      check("no_dropped_listings", not dropped, violations=dropped)]
            record.update(correct=all(c["passed"] for c in checks), checks=checks, clusters=clusters,
                          metrics=confusion_metrics(truth, predictions))
        except Exception as exc:
            record.update(correct=False, checks=[check("grouping_succeeded", False)],
                          error=f"{type(exc).__name__}: {exc}"[:500])
        result.append(record)
    return result


def summary(cases):
    queries = [c for c in cases if c["kind"] == "search"]
    pairs = [c for c in cases if c["kind"] == "matching"]
    matching = {}
    for mode, split in sorted({(c["mode"], c["split"]) for c in pairs}):
        rows = [c for c in pairs if c["mode"] == mode and c["split"] == split]
        decisions = [c for c in rows if c["predicted"] is not None]
        matching[f"{mode}/{split}"] = {
            **confusion_metrics([c["expected"] for c in decisions], [c["predicted"] for c in decisions]),
            "total_cases": len(rows), "execution_errors": len(rows) - len(decisions),
            "decision_coverage": len(decisions) / len(rows),
            "correct_case_rate": sum(c["correct"] for c in rows) / len(rows),
        }
    checks = [check for case in cases for check in case["checks"]]
    successful_queries = [c for c in queries if c["successful"]]
    violating_queries = sum(any(not check["passed"] and check["name"] in {"eligible_entities", "eligible_listing_offers", "serialized_constraints"} for check in c["checks"]) for c in successful_queries)
    returned_groups = sum(c.get("group_count", 0) for c in queries)
    groups = [c for c in cases if c["kind"] == "grouping"]
    grouping_truth, grouping_predictions = [], []
    for case in groups:
        counts = case.get("metrics", {})
        for key, truth, prediction in (("tp", True, True), ("fp", False, True), ("tn", False, False), ("fn", True, False)):
            grouping_truth.extend([truth] * counts.get(key, 0))
            grouping_predictions.extend([prediction] * counts.get(key, 0))
    return {"case_count": len(cases), "search": summarize_search(queries),
            "search_by_surface": grouped_search(queries, "surface"),
            "search_by_category": grouped_search(queries, "category"),
            "matching": matching,
            "grouping": {"scenarios": len(groups), "correct": sum(c["correct"] for c in groups),
                         "execution_errors": sum("metrics" not in c for c in groups),
                         "pairwise": confusion_metrics(grouping_truth, grouping_predictions)},
            "safety": {"successful_query_support": len(successful_queries),
                       "queries_returning_ineligible_entities": violating_queries,
                       "successful_query_constraint_violation_rate": violating_queries / len(successful_queries) if successful_queries else None,
                       "returned_comparison_groups": returned_groups,
                       "incompatible_group_rate": sum(c.get("incompatible_group_count", 0) for c in queries) / returned_groups if returned_groups else None},
            "checks": {"total": len(checks), "failed": sum(not c["passed"] for c in checks),
            "critical_failed": sum(c["critical"] and not c["passed"] for c in checks)},
            "absolute_passed": all(c["passed"] for c in checks)}


def source_fingerprint():
    files = [ROOT / name for name in ("main.py", "db.py", "search_service.py", "normalizer.py", "models.py", "config.py")]
    files += [p for directory in ("src/search", "src/spec", "config", "seeds", "mcp_server")
              for p in (ROOT / directory).rglob("*") if p.suffix in {".py", ".yaml", ".csv", ".txt"}]
    return digest({p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_text(encoding="utf-8-sig").encode()).hexdigest() for p in sorted(files)})


def evaluator_fingerprint():
    directory = Path(__file__).parent
    return digest({p.name: p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.py"))})


async def run_suite(dataset_directory=ROOT / "evaluations"):
    dataset = load_dataset(dataset_directory)
    # The CLI owns process lifetime. Embedding into a running application would
    # mutate its global settings, so reject an already imported server.
    if "main" in sys.modules or "mcp_server.server" in sys.modules:
        raise RuntimeError("Run evaluation in a fresh process; backend servers are already imported.")
    import httpx
    logging.disable(logging.INFO)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="aggregate_quality_") as directory:
        runtime = Runtime(Path(directory), dataset)
        cases = []
        try:
            async with runtime.main.lifespan(runtime.main.web_app), httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.main.web_app, raise_app_exceptions=False), base_url="http://evaluation.invalid") as client:
                for case in dataset["searches"]["queries"]:
                    for surface in case.get("surfaces", ("http", "mcp")):
                        cases.append(await evaluate_query(runtime, client, case, surface))
                for case in dataset["searches"]["contracts"]:
                    cases.append(await evaluate_contract(runtime, client, case))
                for case in dataset["searches"]["pagination"]:
                    cases.append(await evaluate_pagination(runtime, client, case))
            cases.extend(evaluate_matching(runtime))
            cases.extend(evaluate_groups(runtime))
        finally:
            await runtime.close()
            # sqlite3's transaction context manager does not close connections.
            # Some legacy handlers leave connection cycles for GC; collect them
            # before removing the isolated database on Windows.
            gc.collect()
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip() if (ROOT / ".git").exists() else None
    except (OSError, subprocess.CalledProcessError):
        commit = None
    return {
        "report_version": REPORT_VERSION, "metric_version": METRIC_VERSION, "evaluation_completed": True,
        "created_at": datetime.now(timezone.utc).isoformat(), "dataset_sha256": dataset["sha256"],
        "catalog_sha256": dataset["catalog_sha256"], "suite_config": SUITE_CONFIG,
        "evaluator_sha256": evaluator_fingerprint(),
        "source_sha256": source_fingerprint(), "git_commit": commit,
        "environment": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "platform": sys.platform,
                        "packages": {name: version(name) for name in ("fastapi", "pydantic", "mcp", "httpx")}},
        "scope": "Controlled fixture quality and contract regression suite; includes historical matcher labels and a separately reported, human-verified production-label stress slice. Neither is an unbiased production accuracy estimate.",
        "duration_seconds": time.perf_counter() - started, "cases": cases, "summary": summary(cases),
    }
