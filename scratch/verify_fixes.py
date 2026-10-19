"""Verify the Round 5 fixes against the live stdio server."""
import json
import subprocess
import sys

PY = sys.executable
SERVER = r"mcp_server\server.py"


class Client:
    def __init__(self):
        self.p = subprocess.Popen([PY, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        self.id = 0
        self._send({"jsonrpc": "2.0", "id": self._nid(), "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "probe", "version": "1"}}})
        self._read()
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _nid(self):
        self.id += 1
        return self.id

    def _send(self, o):
        self.p.stdin.write(json.dumps(o) + "\n")
        self.p.stdin.flush()

    def _read(self):
        return json.loads(self.p.stdout.readline())

    def call(self, name, args=None):
        self._send({"jsonrpc": "2.0", "id": self._nid(), "method": "tools/call",
                    "params": {"name": name, "arguments": args or {}}})
        r = self._read()["result"]
        return r.get("structuredContent") or json.loads(r["content"][0]["text"])

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


c = Client()
ok = 0
fail = 0


def check(label, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        fail += 1
        print(f"  FAIL  {label}  {detail}")


print("P0-1 numeric enum ordering")
r = c.call("search_components", {"filters": {"voltage": {"gte": 24}}, "limit": 1})
check("voltage gte 24 returns matches", r["ok"] and r["total_matches"] > 0, f"total={r['total_matches']}")
print("        total:", r["total_matches"], "| sample:", r["data"]["results"][0]["specs"].get("voltage"))

r = c.call("search_components", {"filters": {"chemistry": {"gte": "lipo"}}})
check("categorical enum ordering rejected", r["ok"] is False and r.get("error_code") == "operator_not_supported_for_type", r.get("error_code"))
print("        ->", r.get("message"))

r = c.call("search_components", {"filters": {"voltage": {"eq": 24, "tolerance": 0.1}}, "limit": 1})
check("tolerance range on numeric enum works", r["ok"] and r["total_matches"] > 0, r.get("message"))

print("\nP0-2 cursor binding")
p1 = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 2})
cur = p1["next_cursor"]
r = c.call("search_components", {"filters": {"voltage": "12"}, "limit": 2, "cursor": cur})
check("stale cursor rejected", r["ok"] is False and r.get("error_code") == "stale_cursor", r.get("error_code"))
r = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 2, "cursor": "garbage"})
check("garbage cursor rejected", r["ok"] is False and r.get("error_code") == "invalid_cursor", r.get("error_code"))
p2 = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 2, "cursor": cur})
check("valid cursor still pages", p2["ok"] and not ({x["product_id"] for x in p1["data"]["results"]} & {x["product_id"] for x in p2["data"]["results"]}))

print("\nP1-3 limit bounds")
r = c.call("search_components", {"filters": {"voltage": "24"}, "limit": -1})
check("negative limit rejected", r["ok"] is False and r.get("error_code") == "invalid_filter_value", r.get("error_code"))
r = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 500})
check("oversized limit clamped", r["returned"] <= 50 and r["filters_applied"]["limit"] == 50 and "limit_clamped" in [w["code"] for w in r["warnings"]])

print("\nP1-4 unknown != absent != null")
r = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 1})
row = r["data"]["results"][0]
check("specs holds only extracted keys", all(v is not None for v in row["specs"].values()))
check("missing keys enumerated separately", "specs_not_extracted" in row and not (set(row["specs"]) & set(row["specs_not_extracted"])))
print("        specs:", row["specs"])
print("        not extracted:", row["specs_not_extracted"])
print("        result_coverage:", r["data"]["result_coverage"])

print("\nP1-5 warning spam gone")
r = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 1})
check("no blanket low-coverage warning on a large result set", r["warnings"] == [], r["warnings"])

print("\nP1-6 filters_applied completeness")
r = c.call("search_components", {"filters": {"voltage": "24V"}, "query": "motor", "price_max": 6000, "sort": "price_asc", "limit": 3})
a = r["filters_applied"]
check("echoes all effective args", {"filters", "query", "price_max", "in_stock_only", "sort", "limit", "offset"} <= set(a))
check("echoes normalized filter value", a["filters"].get("voltage") == "24", a["filters"])

print("\nP1-7 blank query")
r = c.call("search_components", {"query": "   "})
check("blank query rejected", r["ok"] is False and r.get("error_code") == "empty_query", r.get("error_code"))

print("\nP2-10 describe_catalog envelope")
r = c.call("describe_catalog")
check("returned counts documents", r["returned"] == 1 and r["total_matches"] == r["data"]["counts"]["products"], f"{r['returned']}/{r['total_matches']}")

print("\nP2-11 suggestion ranking")
r = c.call("search_components", {"filters": {"voltage": "99"}})
vals = [float(s["value"]) for s in r["data"]["suggestions"]]
check("nearest value ranks first", vals and vals[0] == max(vals), vals)

print("\nNew tools")
r = c.call("health")
check("health ok", r["ok"] and r["data"]["status"] == "ok" and r["data"]["integrity_check"] == "ok")
print("        ", json.dumps({k: r["data"][k] for k in ("counts", "dependency_versions")})[:220])

r = c.call("search_by_mpn", {"mpn": "RMCS-2077"})
check("search_by_mpn runs", r["ok"] is True)
print("        RMCS-2077 listings:", r["total_matches"], "| warnings:", [w["code"] for w in r["warnings"]])

r = c.call("search_components", {"query": "IG52 160KGCM", "limit": 5})
mpns = [x["mpn"] for x in r["data"]["results"] if x["mpn"]]
r = c.call("search_by_mpn", {"mpn": mpns[0]}) if mpns else {"ok": True, "total_matches": 0}
check("search_by_mpn exact hit", r["ok"] and r["total_matches"] >= 1, mpns[:1])

print("\nDuplicate/listing transparency")
r = c.call("search_components", {"query": "IG52 160KGCM", "limit": 10})
check("listing semantics declared", r["data"].get("items_are") == "listings" and "note" in r["data"])
print("        vendors:", [(x["vendor"], round(x["price"])) for x in r["data"]["results"]])

c.close()
print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
