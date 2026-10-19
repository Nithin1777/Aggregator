import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open('audit_153_parsed.json', 'r', encoding='utf-8') as f:
    entries = json.load(f)

flagged = [7, 24, 26, 29, 38, 40, 53, 59, 61, 65, 66, 67, 104, 113, 114, 115, 116, 117, 119, 121, 122, 123, 125, 126]

for item in entries:
    idx = int(item['idx'])
    if idx > 90:
        break
    if idx in flagged:
        continue
    print(f"[{idx:03d}] {item['v_a']}: {item['title_a']} || {item['v_b']}: {item['title_b']}")

