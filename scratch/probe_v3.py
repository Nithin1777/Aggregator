"""Edge-case probe: does the server fail loudly, or silently return wrong answers?"""
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

print("=== 1. numeric operator on an ENUM key (voltage gte 24) -- should work or error, not 0")
r = c.call("search_components", {"filters": {"voltage": {"gte": 24}}, "limit": 3})
print("   ok:", r["ok"], "| total:", r["total_matches"], "| error:", r.get("error_code"),
      "| warnings:", [w["code"] for w in r.get("warnings", [])])

print("\n=== 2. count_components vs search_components parity")
f = {"filters": {"voltage": "24", "rpm": {"gte": 100, "lte": 200}}}
cnt = c.call("count_components", f)["total_matches"]
srch = c.call("search_components", f)["total_matches"]
print("   count:", cnt, "| search:", srch, "| match:", cnt == srch)

print("\n=== 3. limit=-1 (unbounded?) and limit=500 (doc says max 50)")
for lim in (-1, 500):
    try:
        r = c.call("search_components", {"filters": {"voltage": "24"}, "limit": lim})
        print(f"   limit={lim}: ok={r['ok']} returned={r['returned']} total={r['total_matches']} "
              f"error={r.get('error_code')}")
    except Exception as e:
        print(f"   limit={lim}: EXCEPTION {e}")

print("\n=== 4. specs dict semantics: absent vs null")
r = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 1})
s = r["data"]["results"][0]["specs"]
print("   specs keys:", list(s.keys()))
print("   values:", s)
print("   -> all 7 keys present; a missing spec is null, identical to a genuinely-null value" )

print("\n=== 5. per-result coverage / provenance")
res = r["data"]["results"][0]
print("   result keys:", sorted(res.keys()))
print("   has confidence field per spec:", "confidence" in json.dumps(res["specs"]))
print("   coverage envelope:", r["coverage"])

print("\n=== 6. duplicate part across vendors (no grouping?)")
r = c.call("search_components", {"query": "IG52 160KGCM", "limit": 10})
titles = [(x["vendor"], round(x["price"]), x["title"][:60]) for x in r["data"]["results"]]
for t in titles:
    print("   ", t)
print("   total_matches:", r["total_matches"], "| returned:", r["returned"])

print("\n=== 7. get_product: raw scraped attributes present?")
r = c.call("get_product", {"product_id": 33755})
d = r["data"]
print("   data keys:", sorted(d.keys()))
print("   extracted_specs:", d["extracted_specs"])
print("   description == title?", d["description"] == d["title"])
print("   crawl_timestamps:", d["crawl_timestamps"])
print("   vendor_offers:", len(d["vendor_offers"]))

print("\n=== 8. suggestions quality for unmatched enum")
r = c.call("search_components", {"filters": {"voltage": "99"}})
print("   ok:", r["ok"], "total:", r["total_matches"])
print("   suggestions:", json.dumps(r["data"].get("suggestions"))[:400])
print("   filters_applied:", r["filters_applied"])

print("\n=== 9. does a bad cursor get rejected?")
r1 = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 2})
cur = r1["next_cursor"]
r2 = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 2, "cursor": "not-a-cursor"})
print("   garbage cursor -> ok:", r2["ok"], "first id:", r2["data"]["results"][0]["product_id"] if r2["data"]["results"] else None)
r3 = c.call("search_components", {"filters": {"voltage": "12"}, "limit": 2, "cursor": cur})
print("   cursor from voltage=24 reused with voltage=12 -> first id:", r3["data"]["results"][0]["product_id"] if r3["data"]["results"] else None)

print("\n=== 10. free-text only, no filters (does query work alone?)")
r = c.call("search_components", {"query": "gearbox 24v", "limit": 3})
print("   total:", r["total_matches"], "| first:", r["data"]["results"][0]["title"][:70] if r["data"]["results"] else None)

print("\n=== 11. unknown/blank query handling")
for q in ("", "   ", '"; drop table products;--'):
    r = c.call("search_components", {"query": q, "limit": 2})
    print(f"   query={q!r}: ok={r['ok']} total={r['total_matches']} err={r.get('error_code')}")

c.close()
