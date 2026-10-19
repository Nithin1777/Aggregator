import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from scripts.run_fresh_sample_audit import draw_fresh_candidate_sample
from normalizer import ComponentItem, can_merge

pairs = draw_fresh_candidate_sample(target_pairs=800, seed=43)
merges = []
for idx, (ra, rb) in enumerate(pairs):
    ca = ComponentItem(ra['title'], float(ra['price'] or 0.0), mpn=ra['mpn'])
    cb = ComponentItem(rb['title'], float(rb['price'] or 0.0), mpn=rb['mpn'])
    m, r = can_merge(ca, cb)
    if m:
        merges.append((idx, ra, rb, r))

print(f"Total permitted merges: {len(merges)}")
for idx, ra, rb, r in merges:
    print(f"[{idx:03d}] {r}")
    print(f"  A ({ra['vendor']}, Rs. {ra['price']}): {ra['title']}")
    print(f"  B ({rb['vendor']}, Rs. {rb['price']}): {rb['title']}")
