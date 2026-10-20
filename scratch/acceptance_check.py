"""Final acceptance verification for the unified Aggregate web UI and API."""
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"
DB_PATH = Path("data/components.db")

PASS, FAIL = "PASS", "FAIL"
results = []


def record(name, ok, detail=""):
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" - {detail}" if detail else ""))


def get(path, timeout=60):
    req = urllib.request.Request(BASE_URL + path, headers={"Accept": "*/*"})
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read(), (time.perf_counter() - started) * 1000


print("=" * 72)
print("AGGREGATE ACCEPTANCE VERIFICATION")
print("=" * 72)

# 1. Pages -------------------------------------------------------------------
print("\n--- 1. Pages render ---")
for path, label in [
    ("/", "landing"),
    ("/?q=esp32", "search results"),
    ("/?category=resistors", "category browse"),
    ("/?category=motor", "category with spec filters"),
    ("/categories", "category index"),
    ("/vendors", "vendor index"),
    ("/api/categories", "category api"),
    ("/api/popular", "popular queries"),
    ("/health", "health probe"),
]:
    try:
        status, body, elapsed = get(path)
        record(f"GET {path} ({label})", status == 200, f"{elapsed:.0f} ms, {len(body)} bytes")
    except Exception as exc:
        record(f"GET {path} ({label})", False, str(exc))

# 2. Branding ----------------------------------------------------------------
print("\n--- 2. Branding ---")
status, body, _ = get("/")
html = body.decode("utf-8", "replace")
record("Product is named Aggregate", "Aggregate" in html and "Octopart" not in html)
record("Category filters are rendered", 'data-category-slug="resistors"' in html)
record("Spec filter panel is present", "data-spec-filters" in html)

# 3. Search ------------------------------------------------------------------
print("\n--- 3. Search API ---")
for query in ["arduino", "esp32", "servo", "relay", "dht11"]:
    try:
        status, body, elapsed = get(f"/search?q={urllib.parse.quote(query)}&refresh=off")
        data = json.loads(body)
        ok = status == 200 and len(data.get("comparison_groups", [])) > 0
        record(
            f"search '{query}'",
            ok,
            f"{elapsed:.0f} ms, {data.get('total_results')} items, "
            f"{len(data.get('comparison_groups', []))} groups",
        )
    except Exception as exc:
        record(f"search '{query}'", False, str(exc))

# 4. Categories --------------------------------------------------------------
print("\n--- 4. Category filtering ---")
try:
    status, body, elapsed = get("/search?q=resistor&category=resistors&limit=30&refresh=off")
    data = json.loads(body)
    groups = data.get("comparison_groups", [])
    cats = {g.get("category_canonical") for g in groups}
    record(
        "category filter narrows results to one category",
        bool(groups) and cats == {"resistors"},
        f"{len(groups)} groups, categories={cats}",
    )
except Exception as exc:
    record("category filter narrows results to one category", False, str(exc))

try:
    status, body, _ = get("/api/categories")
    payload = json.loads(body)
    cats = payload.get("categories", [])
    with_specs = [c for c in cats if c.get("filters")]
    record(
        "category api returns the taxonomy with counts",
        len(cats) >= 30 and all("slug" in c and "count" in c for c in cats),
        f"{len(cats)} categories, {payload.get('total')} parts",
    )
    record(
        "categories that carry attributes expose their filters",
        {c["slug"] for c in with_specs} == {"motor", "battery", "relay"},
        ", ".join(f"{c['slug']}({len(c['filters'])})" for c in with_specs),
    )
except Exception as exc:
    record("category api returns the taxonomy with counts", False, str(exc))

# 5. Spec filters ------------------------------------------------------------
print("\n--- 5. Spec-attribute filters ---")
for label, q, spec in [
    ("motor rpm <= 1000", "motor", {"rpm": {"lte": 1000}}),
    ("motor 12V+ and rpm <= 100", "motor", {"voltage": {"gte": 12}, "rpm": {"lte": 100}}),
    ("battery chemistry = lipo", "battery", {"chemistry": {"eq": "lipo"}}),
    ("relay channels >= 4", "relay", {"channels": {"gte": 4}}),
]:
    try:
        url = f"/api/search?q={urllib.parse.quote(q)}&limit=40&refresh=off"
        url += "&spec_filters=" + urllib.parse.quote(json.dumps(spec))
        status, body, elapsed = get(url)
        data = json.loads(body)
        record(
            label,
            status == 200 and data.get("total_results", 0) > 0,
            f"{data.get('total_results')} items ({elapsed:.0f} ms)",
        )
    except Exception as exc:
        record(label, False, str(exc))

try:
    get("/api/search?q=motor&spec_filters=" + urllib.parse.quote(json.dumps({"nope": {"gte": 1}})))
    record("invalid spec filter is rejected with 400", False, "no error raised")
except urllib.error.HTTPError as exc:
    record("invalid spec filter is rejected with 400", exc.code == 400, f"HTTP {exc.code}")

# 6. Suggestions -------------------------------------------------------------
print("\n--- 6. Search suggestions ---")
for prefix in ["esp3", "dht", "lm25", "resis"]:
    try:
        status, body, elapsed = get(f"/api/suggest?q={prefix}")
        payload = json.loads(body)
        suggestions = payload.get("suggestions", [])
        record(
            f"suggestions for '{prefix}'",
            status == 200 and len(suggestions) > 0,
            f"{elapsed:.0f} ms, {len(suggestions)} suggestions",
        )
    except Exception as exc:
        record(f"suggestions for '{prefix}'", False, str(exc))

# 7. Click telemetry ---------------------------------------------------------
print("\n--- 7. Click telemetry ---")
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("DELETE FROM ui_clicks")
conn.commit()
payload = json.dumps(
    {
        "query": "arduino",
        "group_id": "ARDUINO-UNO-R3",
        "vendor": "quartzcomponents",
        "product_id": "8398761820394",
    }
).encode("utf-8")
req = urllib.request.Request(
    f"{BASE_URL}/feedback",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req) as resp:
        post_status = resp.status
    cursor.execute("SELECT COUNT(*) FROM ui_clicks")
    click_count = cursor.fetchone()[0]
    record("POST /feedback records a click", post_status == 200 and click_count == 1)
except Exception as exc:
    record("POST /feedback records a click", False, str(exc))
finally:
    conn.close()

# 8. Static assets -----------------------------------------------------------
print("\n--- 8. Static assets ---")
for path in ["/static/css/aggregate.css", "/static/js/aggregate.js", "/static/icons/favicon.svg"]:
    try:
        status, body, _ = get(path)
        record(f"GET {path}", status == 200, f"{len(body)} bytes")
    except Exception as exc:
        record(f"GET {path}", False, str(exc))

print("\n" + "=" * 72)
failed = [r for r in results if r[1] == FAIL]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
if failed:
    print("\nFailed:")
    for name, _, detail in failed:
        print(f"  - {name}: {detail}")
print("=" * 72)
