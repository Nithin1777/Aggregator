"""
Test runner for Round 3 MCP Server: Spec-Driven Direct Parametric Search.
Executes the 5 rewritten frozen tests against mcp_server.server tools.
"""
import json
import logging
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from mcp_server.server import list_spec_keys, search_components

def run_round3_tests():
    print("=" * 70)
    print("ROUND 3 FROZEN TEST SUITE: SPEC-DRIVEN QUERY SERVER")
    print("=" * 70)

    # 1. Inspect list_spec_keys()
    keys_info = list_spec_keys()
    print(f"\n[DISCOVERY] list_spec_keys() returned {len(keys_info)} advertised spec keys:")
    for k in keys_info:
        r_str = f", range={k['range']}" if k['range'] else ""
        v_str = f", values={k['values']}" if k['values'] else ""
        u_str = f" ({k['unit']})" if k['unit'] else ""
        print(f"  - {k['key']}{u_str}: display='{k['display_name']}', type={k['type']}, count={k['count']}{r_str}{v_str}")

    advertised_key_names = {k["key"] for k in keys_info}

    test_results = []

    # -------------------------------------------------------------
    # TEST 1: "12v 200rpm motor"
    # Call: search(filters={"voltage":"12", "rpm":"190-210"}, query="motor")
    # Expect: >=1 result; all rpm in range, all voltage=12
    # -------------------------------------------------------------
    print("\n" + "-" * 70)
    print("TEST 1: '12v 200rpm motor'")
    assert "voltage" in advertised_key_names and "rpm" in advertised_key_names
    t1_res = search_components(filters={"voltage": "12", "rpm": "190-210"}, query="motor")
    print(f"Result count: {t1_res['count']}")
    
    t1_pass = t1_res['count'] >= 1
    for r in t1_res['results']:
        rpm_val = float(r['specs'].get('rpm', 0))
        volt_val = str(r['specs'].get('voltage', ''))
        if not (190 <= rpm_val <= 210 and volt_val == "12"):
            t1_pass = False
            print(f"FAILED item check: rpm={rpm_val}, volt={volt_val}, title={r['title']}")

    print(f"Test 1 Status: {'PASS' if t1_pass else 'FAIL'}")
    test_results.append({
        "id": "TEST-1",
        "query": "12v 200rpm motor",
        "call": 'search_components(filters={"voltage":"12", "rpm":"190-210"}, query="motor")',
        "passed": t1_pass,
        "count": t1_res['count'],
        "top_5": t1_res['results'][:5]
    })

    # -------------------------------------------------------------
    # TEST 2: "4s 5200mah battery"
    # Call: search(filters={"cells":"4s","capacity_mah":"5100-5300"}, query="battery")
    # Expect: >=1; all capacities in range (5100-5300) and cells="4s"
    # -------------------------------------------------------------
    print("\n" + "-" * 70)
    print("TEST 2: '4s 5200mah battery'")
    assert "cells" in advertised_key_names and "capacity_mah" in advertised_key_names
    t2_res = search_components(filters={"cells": "4s", "capacity_mah": "5100-5300"}, query="battery")
    print(f"Result count: {t2_res['count']}")

    t2_pass = t2_res['count'] >= 1
    for r in t2_res['results']:
        cap_val = float(r['specs'].get('capacity_mah', 0))
        cells_val = str(r['specs'].get('cells', ''))
        if not (5100 <= cap_val <= 5300 and cells_val == "4s"):
            t2_pass = False
            print(f"FAILED item check: cap={cap_val}, cells={cells_val}, title={r['title']}")

    print(f"Test 2 Status: {'PASS' if t2_pass else 'FAIL'}")
    test_results.append({
        "id": "TEST-2",
        "query": "4s 5200mah battery",
        "call": 'search_components(filters={"cells":"4s", "capacity_mah":"5100-5300"}, query="battery")',
        "passed": t2_pass,
        "count": t2_res['count'],
        "top_5": t2_res['results'][:5]
    })

    # -------------------------------------------------------------
    # TEST 3: "motor 150 to 250 rpm, cheapest"
    # Call: search(filters={"rpm":"150-250"}, query="motor", sort="price_asc")
    # Expect: >=1, no result outside 150-250, sorted ascending by price
    # -------------------------------------------------------------
    print("\n" + "-" * 70)
    print("TEST 3: 'motor 150 to 250 rpm, cheapest'")
    t3_res = search_components(filters={"rpm": "150-250"}, query="motor", sort="price_asc")
    print(f"Result count: {t3_res['count']}")

    t3_pass = t3_res['count'] >= 1
    prices = [r['best_price'] for r in t3_res['results'] if r['best_price'] is not None]
    if prices != sorted(prices):
        t3_pass = False
        print(f"FAILED sort check: prices={prices}")

    for r in t3_res['results']:
        rpm_val = float(r['specs'].get('rpm', 0))
        if not (150 <= rpm_val <= 250):
            t3_pass = False
            print(f"FAILED range check: rpm={rpm_val}, title={r['title']}")

    print(f"Test 3 Status: {'PASS' if t3_pass else 'FAIL'}")
    test_results.append({
        "id": "TEST-3",
        "query": "motor 150 to 250 rpm, cheapest",
        "call": 'search_components(filters={"rpm":"150-250"}, query="motor", sort="price_asc")',
        "passed": t3_pass,
        "count": t3_res['count'],
        "top_5": t3_res['results'][:5]
    })

    # -------------------------------------------------------------
    # TEST 4: "motor with phase resistance 2 ohm"
    # list_spec_keys() shows no phase_resistance ->
    # Call: search(query="motor phase resistance 2 ohm", filters={})
    # Expect: valid response, no error
    # -------------------------------------------------------------
    print("\n" + "-" * 70)
    print("TEST 4: 'motor with phase resistance 2 ohm'")
    has_phase_res = "phase_resistance" in advertised_key_names
    print(f"'phase_resistance' in advertised keys: {has_phase_res} (Expected: False)")
    t4_res = search_components(filters={}, query="motor phase resistance 2 ohm")
    print(f"Result count: {t4_res['count']}")
    t4_pass = (not has_phase_res) and isinstance(t4_res, dict) and "results" in t4_res
    print(f"Test 4 Status: {'PASS' if t4_pass else 'FAIL'}")
    test_results.append({
        "id": "TEST-4",
        "query": "motor with phase resistance 2 ohm",
        "call": 'search_components(query="motor phase resistance 2 ohm", filters={})',
        "passed": t4_pass,
        "count": t4_res['count'],
        "top_5": t4_res['results'][:5]
    })

    # -------------------------------------------------------------
    # TEST 5: "24v 50000rpm motor under 50"
    # Call: search(filters={"voltage":"24","rpm":"49000-50000"}, price_max=50, query="motor")
    # Expect: count 0 with clear response
    # -------------------------------------------------------------
    print("\n" + "-" * 70)
    print("TEST 5: '24v 50000rpm motor under 50'")
    t5_res = search_components(filters={"voltage": "24", "rpm": "49000-50000"}, price_max=50, query="motor")
    print(f"Result count: {t5_res['count']}, Hint: {t5_res.get('hint')}")
    t5_pass = t5_res['count'] == 0 and "hint" in t5_res
    print(f"Test 5 Status: {'PASS' if t5_pass else 'FAIL'}")
    test_results.append({
        "id": "TEST-5",
        "query": "24v 50000rpm motor under 50",
        "call": 'search_components(filters={"voltage":"24", "rpm":"49000-50000"}, price_max=50, query="motor")',
        "passed": t5_pass,
        "count": t5_res['count'],
        "top_5": t5_res['results'][:5]
    })

    # Save detailed results for report generation
    with open("data/round3_test_results.json", "w", encoding="utf-8") as f:
        json.dump(test_results, f, indent=2)

    print("\n" + "=" * 70)
    print("ALL 5 FROZEN TESTS FINISHED")
    print("=" * 70)

if __name__ == "__main__":
    run_round3_tests()
