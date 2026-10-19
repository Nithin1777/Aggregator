import re
import json

log_path = r'C:\Users\nithi\.gemini\antigravity\brain\54ea6d64-2aa6-4ae5-9ae9-2904735ca905\.system_generated\tasks\task-1943.log'
with open(log_path, 'r', encoding='utf-8') as f:
    text = f.read()

# Pattern to extract each merge entry
pattern = re.compile(
    r'\[(\d{3})\]\s+\[(.*?)\]\s+Card:\s+(.*?)\n\s+Vendor A \((.*?),\s+Rs\.\s+([\d.]+)\):\s+(.*?)\n\s+Vendor B \((.*?),\s+Rs\.\s+([\d.]+)\):\s+(.*?)\n\s+Verdict:\s+Match=(.*?)\s+\|\s+Score=([\d.]+)\s+\|\s+Reason:\s+(.*?)\n',
    re.DOTALL
)

matches = pattern.findall(text)
print(f"Regex matched {len(matches)} entries.")

entries = []
for m in matches:
    idx, query, card, v_a, p_a, title_a, v_b, p_b, title_b, match_res, score, reason = m
    entries.append({
        'idx': idx,
        'query': query.strip(),
        'card': card.strip(),
        'v_a': v_a.strip(),
        'p_a': float(p_a),
        'title_a': title_a.strip(),
        'v_b': v_b.strip(),
        'p_b': float(p_b),
        'title_b': title_b.strip(),
        'score': float(score),
        'reason': reason.strip()
    })

with open('audit_153_parsed.json', 'w', encoding='utf-8') as f:
    json.dump(entries, f, indent=2)

print("Saved audit_153_parsed.json successfully.")
