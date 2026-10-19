import json
from draft_labels import pairs_analysis

with open('data/gray_sample.jsonl', 'r', encoding='utf-8') as f:
    gray_pairs = [json.loads(line) for line in f if line.strip()][:40]

assert len(gray_pairs) == 40, f"Expected 40 pairs, got {len(gray_pairs)}"
assert len(pairs_analysis) == 40, f"Expected 40 analyses, got {len(pairs_analysis)}"

agent_records = []
for gp, pa in zip(gray_pairs, pairs_analysis):
    assert gp['pair_id'] == pa['pair_id'], f"Mismatch: {gp['pair_id']} vs {pa['pair_id']}"
    rec = {
        'pair_id': gp['pair_id'],
        'title_a': gp['title_a'],
        'title_b': gp['title_b'],
        'label': pa['label'],
        'confidence': pa['confidence'],
        'reason': pa['reason']
    }
    agent_records.append(rec)

with open('data/agent_labels.jsonl', 'w', encoding='utf-8') as f:
    for rec in agent_records:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')

print("Successfully written 40 records to data/agent_labels.jsonl")
