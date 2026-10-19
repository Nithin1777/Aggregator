import json
from collections import Counter

with open('data/judge_seed.jsonl', 'r', encoding='utf-8') as f:
    seed_records = [json.loads(l) for l in f if l.strip()]

with open('data/judge_output.jsonl', 'r', encoding='utf-8') as f:
    judge_records = [json.loads(l) for l in f if l.strip()]

seed_map = {r['pair_id']: r for r in seed_records}
judge_map = {r['pair_id']: r for r in judge_records}

print(f"Seed pairs: {len(seed_records)}, Judge pairs: {len(judge_records)}")

correct = 0
total = len(seed_records)
mismatches = []

classes = ['same', 'different', 'unsure']
cm = {s: {j: 0 for j in classes} for s in classes}

for r in seed_records:
    pid = r['pair_id']
    expected = r['human_label']
    judge_row = judge_map.get(pid, {})
    pred = judge_row.get('label')
    
    if expected in cm and pred in cm[expected]:
        cm[expected][pred] += 1
    
    if pred == expected:
        correct += 1
    else:
        mismatches.append({
            'pair_id': pid,
            'expected': expected,
            'pred': pred,
            'reason': judge_row.get('reason'),
            'deciding_spec': judge_row.get('deciding_spec'),
            'title_a': r['title_a'],
            'title_b': r['title_b']
        })

agreement = (correct / total) * 100.0 if total > 0 else 0.0

print(f"\n=== VALIDATION METRICS ===")
print(f"Total: {total}")
print(f"Agreed: {correct}")
print(f"Mismatches: {len(mismatches)}")
print(f"Agreement Rate: {agreement:.2f}% ({correct}/{total})")

gate_passed = agreement >= 90.0
print(f"Gate Check (>= 90%): {'PASSED' if gate_passed else 'FAILED'}")

print("\nConfusion Matrix (Seed/Human rows x Judge cols):")
print(f"{'seed \\ judge':<15} {'same':<10} {'different':<10} {'unsure':<10}")
for s in classes:
    print(f"{s:<15} {cm[s]['same']:<10} {cm[s]['different']:<10} {cm[s]['unsure']:<10}")

print("\nMismatches Breakdown:")
if not mismatches:
    print("None! 100% agreement.")
else:
    for m in mismatches:
        print(f"- pair_id: {m['pair_id']}")
        print(f"  Expected (Seed): {m['expected']}")
        print(f"  Judge Output:    {m['pred']}")
        print(f"  Deciding Spec:   {m['deciding_spec']}")
        print(f"  Reason:          {m['reason']}")
        print(f"  Title A:         {m['title_a']}")
        print(f"  Title B:         {m['title_b']}\n")
