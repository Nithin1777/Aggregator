import re
import sqlite3

def build_fts_query(clean_q: str) -> str:
    # Remove any quotes or special FTS characters
    sanitized = re.sub(r'["\*\(\)\:]', '', clean_q).strip()
    tokens = sanitized.split()
    if not tokens:
        return ""
    
    variations = []
    
    # 1. Primary exact sequence of tokens
    primary = " ".join([f'"{t}"*' for t in tokens])
    variations.append(f"({primary})")
    
    if len(tokens) > 1:
        # 2. Complete concatenation (e.g. 'smart elex' -> 'smartelex')
        full_concat = "".join(tokens)
        if len(full_concat) >= 3:
            variations.append(f'("{full_concat}"*)')
            
        # 3. Adjacent bigram concatenations (e.g. 'smart elex motor' -> 'smartelex motor')
        if len(tokens) > 2:
            for i in range(len(tokens) - 1):
                merged_bigram = tokens[i] + tokens[i+1]
                sub_tokens = tokens[:i] + [merged_bigram] + tokens[i+2:]
                var_q = " ".join([f'"{t}"*' for t in sub_tokens])
                variations.append(f"({var_q})")
    else:
        # Single token: check letter-digit split (e.g. 'esp32' -> 'esp 32', 'hc05' -> 'hc 05')
        m = re.match(r'^([a-zA-Z]{2,})(\d+)$', tokens[0])
        if m:
            variations.append(f'("{m.group(1)}"* "{m.group(2)}"*)')
            
    # Deduplicate variations while preserving order
    unique_vars = []
    seen = set()
    for v in variations:
        if v not in seen:
            seen.add(v)
            unique_vars.append(v)
            
    return " OR ".join(unique_vars)


conn = sqlite3.connect('data/components.db')
c = conn.cursor()

test_queries = [
    'smart elex',
    'smartelex',
    'smart elex motor',
    'easy mech',
    'easy mech wheel',
    'node mcu',
    'nodemcu',
    'esp 32',
    'esp32',
    'mpu 6050',
    'mpu6050',
    'hc sr04',
    'arduino uno'
]

print("=== TESTING FTS QUERY EXPANSION ===")
for q in test_queries:
    fts_q = build_fts_query(q)
    c.execute('SELECT count(*) FROM products p JOIN products_fts fts ON p.id = fts.rowid WHERE products_fts MATCH ?', (fts_q,))
    count = c.fetchone()[0]
    print(f"'{q}' -> count: {count} | FTS: {fts_q}")
