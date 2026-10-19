import json

with open('data/agent_labels.jsonl', 'r', encoding='utf-8') as f:
    agent_data = [json.loads(l) for l in f if l.strip()]

with open('data/human_labels.jsonl', 'r', encoding='utf-8') as f:
    human_data = [json.loads(l) for l in f if l.strip()]

agent_by_id = {d['pair_id']: d for d in agent_data}
human_by_id = {d['pair_id']: d for d in human_data}

total = len(agent_data)
assert len(human_data) == total

classes = ['same', 'variant', 'different', 'unsure']
cm = {a: {h: 0 for h in classes} for a in classes}

agreed = 0
disagreements = []

conf_stats = {
    'high': {'total': 0, 'agree': 0},
    'medium': {'total': 0, 'agree': 0},
    'low': {'total': 0, 'agree': 0}
}

for a in agent_data:
    pid = a['pair_id']
    h = human_by_id[pid]
    
    a_lbl = a['label']
    h_lbl = h['label']
    conf = a.get('confidence', 'medium')
    
    cm[a_lbl][h_lbl] += 1
    conf_stats[conf]['total'] += 1
    
    if a_lbl == h_lbl:
        agreed += 1
        conf_stats[conf]['agree'] += 1
    else:
        disagreements.append({
            'pair_id': pid,
            'agent_label': a_lbl,
            'agent_reason': a['reason'],
            'human_label': h_lbl,
            'title_a': a['title_a'],
            'title_b': a['title_b']
        })

print(f"Total pairs: {total}")
print(f"Agreement: {agreed}/{total} ({agreed/total*100:.1f}%)")
print(f"Disagreements: {len(disagreements)}")
print("\nConfusion Matrix:")
print(f"{'agent row \\ human col':<20} {'same':<10} {'variant':<10} {'different':<10} {'unsure':<10}")
for a_lbl in classes:
    print(f"agent={a_lbl:<14} {cm[a_lbl]['same']:<10} {cm[a_lbl]['variant']:<10} {cm[a_lbl]['different']:<10} {cm[a_lbl]['unsure']:<10}")

print("\nDisagreements:")
for d in disagreements:
    print(f"- pair_id: {d['pair_id']}")
    print(f"  agent said: {d['agent_label']} ({d['agent_reason']})")
    print(f"  human said: {d['human_label']}")
    print(f"  title_a: {d['title_a']}")
    print(f"  title_b: {d['title_b']}")

print("\nBy-agent-confidence agreement rate:")
for c in ['high', 'medium', 'low']:
    tot = conf_stats[c]['total']
    agr = conf_stats[c]['agree']
    pct = (agr / tot * 100) if tot > 0 else 0.0
    print(f"  {c}: {agr}/{tot} agree ({pct:.1f}%)")
