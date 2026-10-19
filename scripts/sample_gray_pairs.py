"""
Deterministic sampling over matcher_gray_zone for LLM judge evaluation.
Requirements:
- Deterministic SQL + seeded RNG over matcher_gray_zone
- Include merges (0.65-0.90) AND vetoes (>=0.60, non-MPN-veto)
- Strip vendor, price, URL, and the rules' decision before output
- Emit data/gray_sample.jsonl
"""
import json
import random
import sqlite3
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from normalizer import extract_hardware_specs


def sample_gray_zone_pairs(
    db_path: Path = Path("data/components.db"),
    output_path: Path = Path("data/gray_sample.jsonl"),
    sample_size: int = 150,
    seed: int = 42
):
    random.seed(seed)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Query 1: Merges with score 0.65 - 0.90
    c.execute("""
        SELECT id, title_a, title_b, score, decision, reason
        FROM matcher_gray_zone
        WHERE decision IN ('merged', 'match', 'merge')
          AND score >= 0.65 AND score <= 0.90
        ORDER BY id ASC
    """)
    merges = c.fetchall()

    # Query 2: Vetoes with score >= 0.55 and non-MPN veto
    c.execute("""
        SELECT id, title_a, title_b, score, decision, reason
        FROM matcher_gray_zone
        WHERE decision NOT IN ('merged', 'match', 'merge')
          AND score >= 0.55
          AND reason NOT LIKE '%MPN conflict%'
          AND reason NOT LIKE '%Tier A.5%'
          AND reason NOT LIKE '%asymmetric MPN%'
        ORDER BY id ASC
    """)
    vetoes = c.fetchall()

    conn.close()

    print(f"Loaded {len(merges)} eligible merges and {len(vetoes)} eligible non-MPN vetoes from matcher_gray_zone.")

    # Target roughly 50/50 balance if available
    half = sample_size // 2
    sampled_merges = random.sample(merges, min(half, len(merges))) if merges else []
    sampled_vetoes = random.sample(vetoes, min(sample_size - len(sampled_merges), len(vetoes))) if vetoes else []

    all_sampled = sampled_merges + sampled_vetoes
    # Deterministic shuffle with seed
    random.shuffle(all_sampled)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for r in all_sampled:
            pair_id = f"gz_{r['id']}"
            t_a = r["title_a"].strip()
            t_b = r["title_b"].strip()
            specs_a = extract_hardware_specs(t_a)
            specs_b = extract_hardware_specs(t_b)

            # Strip vendor, price, URL, decision, reason per strict prompt instructions
            clean_entry = {
                "pair_id": pair_id,
                "title_a": t_a,
                "specs_a": specs_a,
                "title_b": t_b,
                "specs_b": specs_b
            }
            f.write(json.dumps(clean_entry, ensure_ascii=False) + "\n")
            count += 1

    print(f"Emitted {count} blinded candidate pairs to {output_path} (deterministic seed={seed}).")

if __name__ == "__main__":
    sample_gray_zone_pairs()
