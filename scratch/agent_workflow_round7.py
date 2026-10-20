"""
Round 7 agent-workflow simulation: play out complete questions an agent would ask,
not just single calls, and print a compact transcript of what it sees.

Run:  python scratch/agent_workflow_round7.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server.server as mcp  # noqa: E402


def brief(env):
    d = env.data or {}
    return {
        "ok": env.ok,
        "total": env.total_matches,
        "returned": env.returned,
        "warns": [w.get("code") for w in (env.warnings or [])],
        "effective": (env.filters_applied or {}).get("query_effective"),
    }


def print_step(n, label, env):
    print(f"\n[{n}] {label}")
    print("    ", json.dumps(brief(env)))


def top_rows(env, n=3, fields=("title", "price", "available", "specs")):
    for r in (env.data or {}).get("results", [])[:n]:
        vals = [str(r.get(f))[:60] for f in fields]
        print("      ", " | ".join(vals))


print("=" * 84)
print("WORKFLOW 1  Agent: 'build a small drone; which 3S LiPo packs, ~2200mAh, >=30C, in stock?'")
step = 1
env = mcp.search_components(
    category="battery",
    filters={"cells": "3s", "chemistry": "lipo",
             "capacity_mah": {"gte": 2000, "lte": 2500},
             "discharge_c": {"gte": 30}},
    in_stock_only=True, sort="price_asc", limit=5, refresh=False,
)
print_step(step, "search 3S lipo 2000-2500mAh >=30C in stock (price asc)", env)
top_rows(env, 5, ("title", "price", "available", "specs"))

print("\n" + "=" * 84)
print("WORKFLOW 2  Agent: '12V geared motor, <=400 rpm, >=2 kg-cm torque, cheapest in stock'")
step = 2
env = mcp.search_components(
    category="motor",
    filters={"voltage": "12", "rpm": {"lte": 400}, "torque_kgcm": {"gte": 2}},
    in_stock_only=True, sort="price_asc", limit=5, refresh=False,
)
print_step(step, "search 12V motors <=400rpm >=2kg-cm in stock", env)
top_rows(env, 5, ("title", "price", "available", "specs"))

print("\n" + "=" * 84)
print("WORKFLOW 3  Agent: 'I know the part number MPU6050 - what is it and where is it cheapest?'")
step = 3
env = mcp.search_by_mpn(mpn="MPU6050", limit=10, refresh=False)
print_step(step, "search_by_mpn('MPU6050')", env)
for l in env.data["listings"][:4]:
    print(f"       {l['vendor']:20} {l['price']:>9.2f} in_stock={l['available']} "
          f"specs={l['specs']} sku={l['vendor_sku']}")
step += 1
first_id = env.data["listings"][0]["product_id"]
det = mcp.get_product(product_id=first_id, refresh=False)
print_step(step, f"get_product({first_id}) for full detail", det)
print("       description_source:", det.data["description_source"], "| spec_category:", det.data["spec_category"])

print("\n" + "=" * 84)
print("WORKFLOW 4  Agent (vague): 'battery for my esp32 weather station project'")
step += 1
env = mcp.search_components(query="rechargeable battery for esp32 weather station project",
                            category="battery", limit=3, refresh=False)
print_step(step, "natural-language query + category", env)
top_rows(env, 3, ("title", "specs", "available"))

print("\n" + "=" * 84)
print("WORKFLOW 5  Agent recovery: typos and gibberish must guide, not dead-end")
step += 1
env = mcp.search_components(query="ardunio battry module", limit=3, refresh=False)
print_step(step, "typo query", env)
top_rows(env, 2, ("title",))
step += 1
env = mcp.search_components(query="qqqwerty zzzxcv", limit=3, refresh=False)
print_step(step, "gibberish query", env)
print("       query_suggestions:", env.data.get("query_suggestions"))
step += 1
env = mcp.search_by_mpn(mpn="MPU650", limit=5, refresh=False)
print_step(step, "MPN near-miss", env)
print("       suggested_mpns:", env.data.get("suggested_mpns"))

print("\n" + "=" * 84)
print("WORKFLOW 6  Agent: page through a broad query without duplicate/missing rows")
step += 1
seen = []
cursor = None
for page in range(3):
    env = mcp.search_components(query="sensor module", sort="relevance", limit=10,
                                cursor=cursor, refresh=False)
    rows = [r["product_id"] for r in env.data["results"]]
    seen.extend(rows)
    print(f"    page {page+1}: got={len(rows)} total={env.total_matches} "
          f"truncated={env.truncated} overlap_with_prev={len(set(rows) & set(seen[:-len(rows)]))}")
    cursor = env.next_cursor
    if not cursor:
        break
print("    unique ids across pages:", len(set(seen)), "of", len(seen))

print("\n" + "=" * 84)
print("WORKFLOW 7  Agent guardrails: wrong category / wrong key / empty query")
for label, call in (
    ("category typo", lambda: mcp.search_components(category="batery", refresh=False)),
    ("spec key alias", lambda: mcp.search_components(filters={"torque": {"gte": 2}}, refresh=False)),
    ("blank query", lambda: mcp.search_components(query="   ", refresh=False)),
):
    env = call()
    print(f"    {label:15} -> ok={env.ok} code={env.error_code} "
          f"suggestions={json.dumps(env.suggestions)[:90] if env.suggestions else None}")
