import json

with open('data/gray_sample.jsonl', 'r', encoding='utf-8') as f:
    pairs = [json.loads(line) for line in f if line.strip()][:40]

for idx, p in enumerate(pairs, start=1):
    print(f"[{idx:02d}] {p['pair_id']}")
    print(f"    A: {p['title_a']}")
    print(f"       specs_a: {p.get('specs_a')}")
    print(f"    B: {p['title_b']}")
    print(f"       specs_b: {p.get('specs_b')}")
