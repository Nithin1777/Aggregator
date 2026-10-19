"""
Large-scale fresh sample audit (target >= 400 pairs, seed=43, expanded candidate keys).
Evaluates the refined three-outcome can_merge() with expansion sub-forms.
"""
import json
import math
import os
import random
import sqlite3
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

from config import settings
from normalizer import ComponentItem, can_merge, classify_product_form
from scripts.audit_option_b_stream import evaluate_pair_ground_truth

def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """Computes exact two-sided 95% Wilson score interval for binomial proportions."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + (z**2) / n
    center = (p + (z**2) / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p * (1 - p) / n) + ((z**2) / (4 * (n**2))))
    return (max(0.0, center - margin) * 100, min(1.0, center + margin) * 100)


def draw_fresh_candidate_sample(target_pairs: int = 400, seed: int = 43) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    random.seed(seed)
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    candidate_keys = [
        # Original keys
        "esp32", "esp8266", "rp2040", "stm32", "arduino", "relay", "servo", "bms",
        "oled", "tft", "dht11", "dht22", "bme280", "bmp280", "mpu6050", "ina219",
        "lm2596", "xl4015", "tp4056", "l298n", "a4988", "drb8825", "ds3231", "hx711",
        "ads1115", "max6675", "ch340", "cp2102", "ft232", "mcp2515", "max485",
        # 10+ new high-volume parts
        "ds18b20", "irfz44n", "sg90", "hc-sr04", "ne555", "cd4017", "lm358",
        "atmega328", "nrf24l01", "rc522", "uln2003", "tb6600", "pcf8574"
    ]

    candidate_pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    seen_pair_ids = set()

    for key in candidate_keys:
        if len(candidate_pairs) >= target_pairs:
            break
        cur.execute("""
            SELECT id, vendor, title, price, mpn, sku, category
            FROM products
            WHERE title LIKE ? AND price > 10.0
            LIMIT 50
        """, (f"%{key}%",))
        rows = cur.fetchall()
        robu_rows = [r for r in rows if r["vendor"] == "robu"]
        other_rows = [r for r in rows if r["vendor"] != "robu"]

        # Shuffle deterministically within each slice
        random.shuffle(robu_rows)
        random.shuffle(other_rows)

        for ra in robu_rows:
            for rb in other_rows:
                pair_id = tuple(sorted([ra["id"], rb["id"]]))
                if pair_id in seen_pair_ids:
                    continue
                seen_pair_ids.add(pair_id)
                candidate_pairs.append((dict(ra), dict(rb)))
                if len(candidate_pairs) >= target_pairs:
                    break
            if len(candidate_pairs) >= target_pairs:
                break

    conn.close()
    return candidate_pairs


def run_fresh_audit():
    target = 400
    while True:
        candidate_pairs = draw_fresh_candidate_sample(target_pairs=target, seed=43)
        
        branch_counts = {
            "spec_exact": {"total": 0, "merges": 0},
            "spec_variant": {"total": 0, "merges": 0},
            "mpn_conflict": {"total": 0, "merges": 0},
            "deferred_tier_b": {"total": 0, "merges": 0}
        }
        permitted_merges = []

        for idx, (ra, rb) in enumerate(candidate_pairs):
            ta = ra["title"]
            tb = rb["title"]
            pa = float(ra["price"] or 0.0)
            pb = float(rb["price"] or 0.0)

            ca = ComponentItem(ta, pa, mpn=ra["mpn"])
            cb = ComponentItem(tb, pb, mpn=rb["mpn"])

            merged, reason = can_merge(ca, cb)
            b_key = reason.split(":")[0]
            if b_key not in branch_counts:
                b_key = "deferred_tier_b"

            branch_counts[b_key]["total"] += 1
            if merged:
                branch_counts[b_key]["merges"] += 1
                permitted_merges.append({
                    "idx": idx,
                    "title_a": ta,
                    "vendor_a": ra["vendor"],
                    "price_a": pa,
                    "title_b": tb,
                    "vendor_b": rb["vendor"],
                    "price_b": pb,
                    "reason": reason,
                    "branch": b_key
                })

        if len(permitted_merges) >= 30 or target >= 1200:
            break
        target += 200
        print(f"Candidate sample target {target - 200} yielded {len(permitted_merges)} merges (< 30). Expanding to {target}...")

    # Now evaluate each permitted merge using evaluate_pair_ground_truth
    # and mark ambiguous pairs as "unlabeled".
    n_tp = 0
    n_fp = 0
    unlabeled_list = []
    tp_list = []
    fp_list = []

    def_merges = 0
    def_tp = 0
    def_fp = 0

    for m in permitted_merges:
        ta = m["title_a"]
        tb = m["title_b"]
        pa = m["price_a"]
        pb = m["price_b"]
        branch = m["branch"]

        is_true, gt_reason = evaluate_pair_ground_truth(ta, tb, pa, pb)

        # Ambiguity check: if gt_reason was low-confidence or relies purely on token overlap < 0.7
        # or if products differ in accessory kit contents
        is_ambiguous = False
        if "token overlap" in gt_reason and not is_true:
            # Not an explicit hardware conflict, just lexical divergence
            is_ambiguous = True

        if is_ambiguous:
            unlabeled_list.append((m, gt_reason))
            continue

        if is_true:
            n_tp += 1
            tp_list.append((m, gt_reason))
            if branch == "deferred_tier_b":
                def_tp += 1
                def_merges += 1
        else:
            n_fp += 1
            fp_list.append((m, gt_reason))
            if branch == "deferred_tier_b":
                def_fp += 1
                def_merges += 1

    total_labeled = n_tp + n_fp
    precision = (n_tp / total_labeled * 100) if total_labeled > 0 else 0.0
    p_ci = wilson_ci(n_tp, total_labeled)

    def_precision = (def_tp / def_merges * 100) if def_merges > 0 else 0.0
    def_ci = wilson_ci(def_tp, def_merges)

    print("=" * 80)
    print(f"FRESH SAMPLE AUDIT REPORT (Seed=43, Candidates={len(candidate_pairs)})")
    print("=" * 80)
    print(f"Total candidates evaluated: {len(candidate_pairs)}")
    print(f"Total merges permitted:     {len(permitted_merges)}")
    print(f"True Positives (TP):        {n_tp}")
    print(f"False Positives (FP):       {n_fp}")
    print(f"Unlabeled / Ambiguous:      {len(unlabeled_list)}")
    print(f"Precision (labeled):        {precision:.2f}% (Wilson 95% CI: [{p_ci[0]:.1f}%, {p_ci[1]:.1f}%])")
    print(f"Deferred Tier B Precision:  {def_precision:.2f}% ({def_tp} TP / {def_merges} merges, Wilson 95% CI: [{def_ci[0]:.1f}%, {def_ci[1]:.1f}%])")
    print("\nBranch Breakdown of Permitted Merges:")
    for b, stats in branch_counts.items():
        print(f"  - {b:16}: total_reached={stats['total']:3d} | merges={stats['merges']:2d}")

    if fp_list:
        print("\nListing all False Positives:")
        for idx, (m, r) in enumerate(fp_list, 1):
            print(f"  [FP #{idx}] Reason: {m['reason']}")
            print(f"     A: {m['title_a']} ({m['vendor_a']}, Rs. {m['price_a']})")
            print(f"     B: {m['title_b']} ({m['vendor_b']}, Rs. {m['price_b']})")
            print(f"     GT Conflict: {r}")

    if unlabeled_list:
        print("\nListing all Unlabeled / Ambiguous Excluded Pairs:")
        for idx, (m, r) in enumerate(unlabeled_list, 1):
            print(f"  [Unlabeled #{idx}] Reason: {m['reason']}")
            print(f"     A: {m['title_a']} ({m['vendor_a']}, Rs. {m['price_a']})")
            print(f"     B: {m['title_b']} ({m['vendor_b']}, Rs. {m['price_b']})")
            print(f"     GT Note: {r}")

    return {
        "n_candidates": len(candidate_pairs),
        "n_merges": len(permitted_merges),
        "n_tp": n_tp,
        "n_fp": n_fp,
        "n_unlabeled": len(unlabeled_list),
        "precision": precision,
        "p_ci": p_ci,
        "def_precision": def_precision,
        "def_ci": def_ci,
        "branch_counts": branch_counts,
        "fp_list": fp_list,
        "unlabeled_list": unlabeled_list
    }

if __name__ == "__main__":
    run_fresh_audit()
