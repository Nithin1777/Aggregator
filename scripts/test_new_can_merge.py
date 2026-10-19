import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from normalizer import ComponentItem, match_components_v2

with open('audit_153_parsed.json', 'r', encoding='utf-8') as f:
    items = json.load(f)

for idx, it in enumerate(items[:20]):
    ca = ComponentItem(it['title_a'])
    cb = ComponentItem(it['title_b'])
    print(f"[{idx+1}]")
    print(f"  A: {it['title_a'][:50]} | mpn={ca.mpn} | spec_sig={ca.spec_sig}")
    print(f"  B: {it['title_b'][:50]} | mpn={cb.mpn} | spec_sig={cb.spec_sig}")
