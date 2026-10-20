"""
Agent-realistic probe of the MCP + web search surface.

Simulates how an LLM agent would answer questions like:
  "Can I use this 12V 7Ah battery for my camera rig?"
  "Find me a 3.7V 2000mAh LiPo with a JST connector"
  "Will this motor work for a 12V 300RPM gearbox?"

Prints compact diagnostics for each step so failure clusters can be reviewed.
Run:  python scratch/agent_probe.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server.server as mcp  # noqa: E402


def brief(env, keys=("total_matches", "returned", "truncated", "next_cursor")):
    out = {k: getattr(env, k) for k in keys}
    if not env.ok:
        out["error_code"] = env.error_code
        out["message"] = env.message
        out["suggestions"] = env.suggestions
    else:
        data = env.data or {}
        if "results" in data:
            out["n_results"] = len(data["results"])
        if "count" in data:
            out["count"] = data["count"]
        if "suggestions" in data and data["suggestions"]:
            out["suggestions"] = data["suggestions"][:5]
    return out


def show(label, env, extra=None):
    print(f"\n--- {label}")
    print(json.dumps(brief(env), ensure_ascii=False))
    if extra and env.ok:
        print(extra(env))
    warnings = [w.get("code") for w in (env.warnings or [])]
    if warnings:
        print("warnings:", warnings)


def titles(env, n=5):
    return [r["title"][:80] for r in (env.data or {}).get("results", [])[:n]]


def specs_of_first(env):
    rows = (env.data or {}).get("results", [])
    if not rows:
        return "no rows"
    r = rows[0]
    return {
        "title": r["title"][:70],
        "specs": r["specs"],
        "not_extracted": r.get("specs_not_extracted"),
        "mpn": r.get("mpn"),
    }


print("=" * 78)
print("SCENARIO 1: battery by spec filters (3.7V 2000mAh LiPo)")
env = mcp.search_components(filters={"chemistry": "lipo", "voltage": "3.7",
                                     "capacity_mah": {"gte": 1500}}, limit=5)
show("1a search_components(filters chemistry=lipo, voltage=3.7, capacity>=1500)", env,
     lambda e: titles(e))

print("=" * 78)
print("SCENARIO 2: natural-language query (agent-style)")
for q in ("battery to power arduino uno for 8 hours",
          "12v 7ah battery for camera",
          "rechargeable battery for esp32 project"):
    env = mcp.search_components(query=q, limit=5)
    show(f"2 search_components(query={q!r})", env, lambda e: titles(e))

print("=" * 78)
print("SCENARIO 3: MPN resolution & typos")
for mpn in ("RMCS-2077", "ESP32-WROOM-32", "MPU6050", "ESP32 WROOM 32", "mpu650"):
    env = mcp.search_by_mpn(mpn=mpn, limit=5)
    show(f"3 search_by_mpn({mpn!r})", env, lambda e: [l["title"][:70] for l in (e.data or {}).get("listings", [])])

print("=" * 78)
print("SCENARIO 4: text query with typo (agent does this constantly)")
for q in ("esp32 wromm", "mpu650 gyro", "battry 18650"):
    env = mcp.search_components(query=q, limit=5)
    show(f"4 search_components(query={q!r})", env, lambda e: titles(e))

print("=" * 78)
print("SCENARIO 5: motor fit (12V, <=1000 rpm, torque mention)")
env = mcp.count_components(filters={"voltage": "12", "rpm": {"lte": 1000}}, query="motor")
show("5a count_components(voltage=12, rpm<=1000, query=motor)", env)
env = mcp.search_components(filters={"voltage": "12", "rpm": {"lte": 1000}}, query="motor", limit=5)
show("5b search_components(same)", env, lambda e: [(r["title"][:60], r["specs"].get("rpm"), r["specs"].get("voltage")) for r in e.data["results"]])
env = mcp.search_components(filters={"torque": {"gte": 2}}, query="motor", limit=5)
show("5c search_components(torque>=2) -- common need", env)

print("=" * 78)
print("SCENARIO 6: battery without spec filters, then inspect detail")
env = mcp.search_components(query="lipo battery", limit=3)
show("6a search_components(query='lipo battery')", env, lambda e: titles(e))
if env.ok and env.data.get("results"):
    pid = env.data["results"][0]["product_id"]
    det = mcp.get_product(product_id=pid, refresh=False)
    print("\n6b get_product first battery -> specs:")
    print(json.dumps(specs_of_first(type("E", (), {"data": {"results": [det.data]}})()) if False else {
        "title": det.data["title"][:70],
        "extracted_specs": det.data.get("extracted_specs"),
        "specs_not_extracted": det.data.get("specs_not_extracted", [])[:12],
        "vendor_offers": len(det.data.get("vendor_offers", [])),
        "raw_response_bytes": len(det.model_dump_json()),
    }, ensure_ascii=False, default=str))

print("=" * 78)
print("SCENARIO 7: batteries, what spec keys exist in practice")
from collections import Counter  # noqa: E402
env = mcp.search_components(query="battery", limit=50)
if env.ok:
    c = Counter()
    for r in env.data["results"]:
        for k in r["specs"]:
            c[k] += 1
    print("7a spec keys present across 50 'battery' listings:", dict(c))
    print("7b sample specs:", [r["specs"] for r in env.data["results"][:4]])

print("=" * 78)
print("SCENARIO 8: pagination stability")
env1 = mcp.search_components(query="sensor", limit=5)
show("8a page 1", env1, lambda e: [r["product_id"] for r in e.data["results"]])
if env1.next_cursor:
    env2 = mcp.search_components(query="sensor", limit=5, cursor=env1.next_cursor)
    show("8b page 2", env2, lambda e: [r["product_id"] for r in e.data["results"]])
    env3 = mcp.search_components(query="sensor", limit=5, cursor=env1.next_cursor)
    print("8c repeat page 2 stable:", env3.ok and [r["product_id"] for r in env3.data["results"]])
else:
    print("8b no cursor (unexpected)")

print("=" * 78)
print("SCENARIO 9: response size (context cost per agent call)")
for label, env in (("search_components limit=10", mcp.search_components(query="motor", limit=10, refresh=False)),
                   ("search_components limit=50", mcp.search_components(query="motor", limit=50, refresh=False)),
                   ("count_components", mcp.count_components(query="motor")),
                   ("describe_catalog", mcp.describe_catalog())):
    print(f"9 {label}: {len(env.model_dump_json()):,} bytes")
