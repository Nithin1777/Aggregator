import json
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


def get(path, timeout=30):
    url = BASE + path
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        el = (time.perf_counter() - t) * 1000
        return resp.status, body, el


def show(label, path, show_head=0):
    try:
        status, body, el = get(path)
        text = body.decode("utf-8", "replace")
        print(f"[{status}] {label:<34} {el:7.1f} ms  len={len(body):>7}")
        if show_head:
            print("      " + text[:show_head].replace("\n", " ")[:300])
        return text
    except Exception as exc:
        print(f"[ERR] {label:<34} {exc}")
        return ""


print("=== pages ===")
for label, path in [
    ("/", "/"),
    ("/?q=esp32", "/?q=esp32"),
    ("/?category=resistors", "/?category=resistors"),
    ("/categories", "/categories"),
    ("/vendors", "/vendors"),
    ("/static/css/aggregate.css", "/static/css/aggregate.css"),
    ("/static/js/aggregate.js", "/static/js/aggregate.js"),
    ("/static/icons/favicon.svg", "/static/icons/favicon.svg"),
]:
    show(label, path)

print()
print("=== api ===")
for label, path in [
    ("/api/categories", "/api/categories"),
    ("/api/suggest?q=esp", "/api/suggest?q=esp"),
    ("/api/suggest?q=dht", "/api/suggest?q=dht"),
    ("/api/suggest?q=resis", "/api/suggest?q=resis"),
    ("/api/popular", "/api/popular"),
]:
    text = show(label, path)
    if text and path.startswith("/api/suggest"):
        try:
            data = json.loads(text)
            for s in data["suggestions"][:6]:
                print(f"        - [{s['type']}] {s['label'][:60]}")
        except Exception as exc:
            print("      parse error", exc)

print()
print("=== search ===")
for q in ["esp32", "dht11"]:
    t = time.perf_counter()
    status, body, el = get(f"/api/search?q={q}&limit=20&refresh=off")
    data = json.loads(body)
    print(
        f"[{status}] q={q:<8} {el:7.1f} ms  items={data['total_results']:<3} "
        f"groups={len(data['comparison_groups']):<3} "
        f"cat={(data['comparison_groups'][0].get('category_canonical') if data['comparison_groups'] else '-')}"
    )

print()
status, body, el = get("/api/search?q=resistor&category=resistors&limit=20&refresh=off")
data = json.loads(body)
print(f"category filter -> items={data['total_results']} groups={len(data['comparison_groups'])}")
cats = set(g.category_canonical for g in data["comparison_groups"])
print("categories in results:", cats)
