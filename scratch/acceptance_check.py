import urllib.request
import json
import time
import sqlite3
from pathlib import Path

BASE_URL = "http://127.0.0.1:8000"
DB_PATH = Path("data/components.db")

print("=" * 70)
print("RUNNING FINAL ACCEPTANCE VERIFICATION")
print("=" * 70)

# 1. Ten Arbitrary Queries Latency & 200 OK Check
test_queries = [
    "arduino", "esp32", "servo", "relay", "dht11",
    "bme280", "lm2596", "nema17", "rp2040", "stm32"
]

print("\n--- 1. Ten Queries Latency (<1.0s) & 200 OK Check ---")
all_latency_passed = True
for q in test_queries:
    start = time.perf_counter()
    url = f"{BASE_URL}/search?q={urllib.parse.quote(q)}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req) as resp:
        elapsed = time.perf_counter() - start
        status = resp.status
        data = json.loads(resp.read().decode("utf-8"))
        total = data.get("total_results", 0)
        groups = len(data.get("comparison_groups", []))
        passed = (status == 200) and (elapsed < 1.0)
        if not passed:
            all_latency_passed = False
        print(f"Query: {q:<12} | Status: {status} | Latency: {elapsed*1000:6.2f} ms ({elapsed:5.3f}s) | Groups: {groups:2d} | Items: {total:3d} | {'PASS' if passed else 'FAIL'}")

print(f"Ten queries check: {'PASS' if all_latency_passed else 'FAIL'}")

# 2. Arduino Uno R3 is first result card
print("\n--- 2. Arduino First Result Check ---")
with urllib.request.urlopen(f"{BASE_URL}/search?q=arduino") as resp:
    data = json.loads(resp.read().decode("utf-8"))
    first_group = data["comparison_groups"][0]
    canonical_first = first_group["canonical_name"]
    first_has_uno_r3 = "uno r3" in canonical_first.lower()
    print(f"First comparison group canonical name: \"{canonical_first}\"")
    print(f"Uno R3 in first card: {first_has_uno_r3} ({'PASS' if first_has_uno_r3 else 'FAIL'})")

# 3. LM2596 Wide Price Warning Bar
print("\n--- 3. LM2596 Wide Price Warning Bar Check ---")
with urllib.request.urlopen(f"{BASE_URL}/search?q=lm2596") as resp:
    data = json.loads(resp.read().decode("utf-8"))
    warning_triggered = False
    for idx, g in enumerate(data["comparison_groups"]):
        in_stock_prices = [o["price"] for o in g["offers"] if o["available"] and o["price"] > 0]
        if len(in_stock_prices) > 1:
            min_p = min(in_stock_prices)
            max_p = max(in_stock_prices)
            ratio = max_p / max(min_p, 1.0)
            if ratio > 3.5:
                warning_triggered = True
                print(f"Card [{idx+1}] \"{g['canonical_name']}\": In-stock range: Rs. {min_p:.2f} to Rs. {max_p:.2f} (ratio: {ratio:.2f}x > 3.5) -> Amber Warning Triggered!")
                break
    print(f"LM2596 wide-price warning: {'PASS' if warning_triggered else 'FAIL'}")

# 4. Click Telemetry: POST /feedback
print("\n--- 4. Click Telemetry Check ---")
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("DELETE FROM ui_clicks")
conn.commit()

feedback_payload = json.dumps({
    "query": "arduino",
    "group_id": "ARDUINO-UNO-R3",
    "vendor": "quartzcomponents",
    "product_id": "8398761820394"
}).encode("utf-8")

req = urllib.request.Request(
    f"{BASE_URL}/feedback",
    data=feedback_payload,
    headers={"Content-Type": "application/json"},
    method="POST"
)

with urllib.request.urlopen(req) as resp:
    post_status = resp.status
    resp_body = json.loads(resp.read().decode("utf-8"))
    print(f"POST /feedback status: {post_status}, response: {resp_body}")

cursor.execute("SELECT COUNT(*) FROM ui_clicks")
click_count = cursor.fetchone()[0]
cursor.execute("SELECT id, ts, query, group_id, vendor, product_id FROM ui_clicks")
row = cursor.fetchone()
print(f"SELECT COUNT(*) FROM ui_clicks: {click_count} (Expected: 1)")
print(f"Recorded row: {row}")
print(f"Click telemetry check: {'PASS' if click_count == 1 else 'FAIL'}")
conn.close()

# 5. Check GET / serves HTML
print("\n--- 5. GET / Web Page Check ---")
with urllib.request.urlopen(f"{BASE_URL}/") as resp:
    html_status = resp.status
    html_text = resp.read().decode("utf-8")
    has_bootstrap = "bootstrap.min.css" in html_text
    has_octopart = "Octopart" in html_text
    has_honesty_heading = "Compare offers" in html_text
    has_warning_js = "Wide price spread" in html_text
    has_telemetry_js = "track-view-btn" in html_text
    
    print(f"GET / status: {html_status}")
    print(f"Bootstrap CDN present: {has_bootstrap}")
    print(f"Octopart branding present: {has_octopart}")
    print(f"Honesty 'Compare offers' present: {has_honesty_heading}")
    print(f"Warning bar logic present: {has_warning_js}")
    print(f"Click telemetry logic present: {has_telemetry_js}")
    all_html_passed = html_status == 200 and has_bootstrap and has_octopart and has_honesty_heading and has_warning_js and has_telemetry_js
    print(f"Web UI check: {'PASS' if all_html_passed else 'FAIL'}")
