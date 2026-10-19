"""Minimal stdio JSON-RPC client to exercise the octopart MCP server the way an agent would."""
import json
import subprocess
import sys

PY = sys.executable
SERVER = r"mcp_server\server.py"


class Client:
    def __init__(self):
        self.p = subprocess.Popen(
            [PY, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self.id = 0
        self._send({"jsonrpc": "2.0", "id": self._nid(), "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "probe", "version": "1"}}})
        self._read()
        self._notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _nid(self):
        self.id += 1
        return self.id

    def _send(self, obj):
        self.p.stdin.write(json.dumps(obj) + "\n")
        self.p.stdin.flush()

    def _notify(self, obj):
        self._send(obj)

    def _read(self):
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        return json.loads(line)

    def call(self, name, args=None):
        self._send({"jsonrpc": "2.0", "id": self._nid(), "method": "tools/call",
                    "params": {"name": name, "arguments": args or {}}})
        return self._read()

    def list_tools(self):
        self._send({"jsonrpc": "2.0", "id": self._nid(), "method": "tools/list", "params": {}})
        return self._read()

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


def summarize(label, resp):
    if "error" in resp:
        print(f"\n### {label}\n  TRANSPORT ERROR: {resp['error']}")
        return None
    res = resp["result"]
    blocks = res.get("content", [])
    sc = res.get("structuredContent")
    print(f"\n### {label}")
    print(f"  content blocks: {len(blocks)} | structuredContent: {'yes' if sc else 'NO'}")
    payload = None
    if sc:
        payload = sc
    elif blocks:
        try:
            payload = json.loads(blocks[0]["text"])
        except Exception:
            print("  raw:", blocks[0]["text"][:300])
            return None
    if payload is not None:
        print("  envelope keys:", sorted(payload.keys()))
        if "ok" in payload:
            print("  ok:", payload["ok"], "| total_matches:", payload.get("total_matches"),
                  "| returned:", payload.get("returned"), "| truncated:", payload.get("truncated"))
            if payload.get("error_code"):
                print("  error_code:", payload["error_code"])
                print("  message:", str(payload.get("message"))[:220])
            if payload.get("warnings"):
                print("  warnings:", [(w.get("code")) for w in payload["warnings"]])
    return payload


def main():
    c = Client()
    print("=== tools/list")
    tl = c.list_tools()
    for t in tl["result"]["tools"]:
        req = t["inputSchema"].get("required", [])
        print(f"  {t['name']:<20} required={req}")

    summarize("describe_catalog()", c.call("describe_catalog"))
    summarize("describe_spec('voltage')", c.call("describe_spec", {"key": "voltage"}))
    summarize("describe_spec('torque_kgcm')  [expected: structured error]",
              c.call("describe_spec", {"key": "torque_kgcm"}))
    summarize("count_components(voltage=24V, price<=5600)",
              c.call("count_components", {"filters": {"voltage": "24V"}, "price_max": 5600}))
    summarize("search_components(voltage=24V, limit=3)",
              c.call("search_components", {"filters": {"voltage": "24V"}, "limit": 3}))
    summarize("search_components(unknown key 'torque')",
              c.call("search_components", {"filters": {"torque": "160"}}))
    summarize("search_components(bad sort)",
              c.call("search_components", {"sort": "cheapest"}))
    summarize("search_components(deprecated range string)",
              c.call("search_components", {"filters": {"rpm": "190-210"}}))
    summarize("search_components(operator form rpm 190-210)",
              c.call("search_components", {"filters": {"rpm": {"gte": 190, "lte": 210}}, "limit": 2}))
    summarize("search_components(enum value not present)",
              c.call("search_components", {"filters": {"voltage": "99"}}))
    summarize("get_product(1)", c.call("get_product", {"product_id": 1}))
    summarize("get_product(999999999)", c.call("get_product", {"product_id": 999999999}))
    summarize("list_spec_keys()", c.call("list_spec_keys"))

    # pagination continuity
    p1 = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 2})
    j1 = p1["result"].get("structuredContent", {})
    cur = j1.get("next_cursor")
    print("\n### pagination")
    print("  page1 ids:", [r["product_id"] for r in j1["data"]["results"]], "cursor:", cur)
    if cur:
        p2 = c.call("search_components", {"filters": {"voltage": "24"}, "limit": 2, "cursor": cur})
        j2 = p2["result"].get("structuredContent", {})
        print("  page2 ids:", [r["product_id"] for r in j2["data"]["results"]],
              "total:", j2.get("total_matches"))
        print("  overlap:", set(r["product_id"] for r in j1["data"]["results"]) &
              set(r["product_id"] for r in j2["data"]["results"]))
        # reuse cursor with different filters
        p3 = c.call("search_components", {"filters": {"voltage": "12"}, "limit": 2, "cursor": cur})
        j3 = p3["result"].get("structuredContent", {})
        print("  cursor reused with different filters -> first id:",
              j3["data"]["results"][0]["product_id"] if j3["data"]["results"] else None,
              "| warnings:", [w.get("code") for w in j3.get("warnings", [])])

    c.close()


if __name__ == "__main__":
    main()
