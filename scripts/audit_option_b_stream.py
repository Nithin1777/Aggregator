"""
Deterministic 200-pair merge-stream audit comparing Matcher V2 vs Option B can_merge().
Measures empirical precision, recall, and merge volume delta across both:
1. The 153-pair previous merge audit stream (apples-to-apples comparison on 79.74% baseline).
2. A fresh, deterministic 200-pair cross-vendor candidate stream drawn from data/components.db.
"""
import json
import os
import re
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
from normalizer import (
    can_merge,
    ComponentItem,
    match_components_v2,
    extract_spec_signature,
    classify_product_form,
    extract_mpn,
    canonical_base_mpn
)

def evaluate_pair_ground_truth(title_a: str, title_b: str, price_a: float, price_b: float) -> Tuple[bool, str]:
    """
    Independent ground-truth evaluator based on physical electronics equivalence.
    Returns (is_true_same_product, reason).
    """
    ta = title_a.lower()
    tb = title_b.lower()

    # 1. Obvious hardware conflict checks
    # Channels
    ch_a = re.findall(r'\b(\d+)\s*(?:channel|ch|way)\b', ta)
    ch_b = re.findall(r'\b(\d+)\s*(?:channel|ch|way)\b', tb)
    if ch_a and ch_b and ch_a[0] != ch_b[0]:
        return False, f"Channel count mismatch: {ch_a[0]} vs {ch_b[0]}"

    # Battery Series
    s_a = re.findall(r'\b([1-6])s\b', ta)
    s_b = re.findall(r'\b([1-6])s\b', tb)
    if s_a and s_b and s_a[0] != s_b[0]:
        return False, f"Battery series mismatch: {s_a[0]}S vs {s_b[0]}S"

    # Battery Chemistry
    if ('lifepo4' in ta and 'nmc' in tb) or ('nmc' in ta and 'lifepo4' in tb):
        return False, "Battery chemistry conflict: LiFePO4 vs NMC"

    # Amperage / Current ratings on power / relay / BMS / CT
    a_amps = set(re.findall(r'\b(\d+(?:\.\d+)?)\s*a\b', ta))
    b_amps = set(re.findall(r'\b(\d+(?:\.\d+)?)\s*a\b', tb))
    if a_amps and b_amps and not (a_amps & b_amps):
        if any(k in ta for k in ['relay', 'bms', 'transformer', 'switch', 'ssr', 'power']):
            return False, f"Current rating mismatch: {a_amps} vs {b_amps}"

    # Voltage (5V vs 12V vs 24V vs 3.3V)
    va = set(re.findall(r'\b(3\.3v|5v|12v|24v|48v|220v)\b', ta))
    vb = set(re.findall(r'\b(3\.3v|5v|12v|24v|48v|220v)\b', tb))
    if va and vb and not (va & vb):
        return False, f"Operating voltage mismatch: {va} vs {vb}"

    # Screen size
    sca = re.findall(r'\b(\d+(?:\.\d+)?)\s*(?:inch|\")\b', ta)
    scb = re.findall(r'\b(\d+(?:\.\d+)?)\s*(?:inch|\")\b', tb)
    if sca and scb and sca[0] != scb[0]:
        return False, f"Screen size mismatch: {sca[0]} vs {scb[0]}"

    # Bare module vs with display
    if (('display' in ta or 'lcd' in ta or 'oled' in ta) != ('display' in tb or 'lcd' in tb or 'oled' in tb)):
        if 'board' in ta or 'board' in tb or 'esp32' in ta or 'esp32' in tb:
            return False, "Embedded display presence asymmetry"

    # Package footprint (DIP vs SMD/SOP)
    if ('dip' in ta and 'sop' in tb) or ('soic' in ta and 'dip' in tb):
        return False, "Package footprint mismatch: DIP vs SOP/SOIC"

    # Board model (Uno vs Nano vs Mega vs Pico)
    boards = ['uno', 'nano', 'mega', 'pico', 'nodemcu', 'esp32-cam']
    for b in boards:
        if (b in ta) != (b in tb):
            return False, f"Board model mismatch for {b}"

    # MPN conflict
    mpn_a = canonical_base_mpn(extract_mpn(title_a))
    mpn_b = canonical_base_mpn(extract_mpn(title_b))
    if mpn_a and mpn_b and mpn_a != mpn_b:
        return False, f"Distinct canonical MPNs: {mpn_a} vs {mpn_b}"

    # If canonical MPNs match and no spec conflicts, it's the same part
    if mpn_a and mpn_b and mpn_a == mpn_b:
        return True, f"Identical canonical MPN {mpn_a}"

    # Token overlap check for generic items
    tokens_a = set(re.findall(r'\w+', ta)) - {'for', 'and', 'with', 'the', 'module', 'board', 'diy', 'good', 'quality', 'in', 'india'}
    tokens_b = set(re.findall(r'\w+', tb)) - {'for', 'and', 'with', 'the', 'module', 'board', 'diy', 'good', 'quality', 'in', 'india'}
    overlap = len(tokens_a & tokens_b) / max(len(tokens_a | tokens_b), 1)
    if overlap >= 0.65:
        return True, f"High token overlap ({overlap:.2f})"

    return False, f"Insufficient token overlap ({overlap:.2f})"


def audit_153_stream() -> Dict[str, Any]:
    """Evaluates Option B on the exact 153 pairs from the prior audit."""
    if not os.path.exists("audit_153_parsed.json"):
        return {}

    with open("audit_153_parsed.json", "r", encoding="utf-8") as f:
        items = json.load(f)

    v2_tp = 0
    v2_fp = 0
    opt_b_tp = 0
    opt_b_fp = 0
    opt_b_blocked_tp = 0
    opt_b_blocked_fp = 0

    reasons_split_tp = {}

    for item in items:
        ta = item['title_a']
        tb = item['title_b']
        pa = float(item.get('price_a', 0.0) or 0.0)
        pb = float(item.get('price_b', 0.0) or 0.0)

        is_true_same, gt_reason = evaluate_pair_ground_truth(ta, tb, pa, pb)
        m_v2, score_v2, reason_v2 = match_components_v2(ta, tb, pa, pb)

        ca = ComponentItem(ta, pa)
        cb = ComponentItem(tb, pb)
        m_opt_b = can_merge(ca, cb)

        if m_v2:
            if is_true_same:
                v2_tp += 1
            else:
                v2_fp += 1

        if m_opt_b:
            if is_true_same:
                opt_b_tp += 1
            else:
                opt_b_fp += 1
        else:
            if is_true_same:
                opt_b_blocked_tp += 1
                # Why was TP blocked?
                if not ca.mpn or not cb.mpn or ca.mpn != cb.mpn:
                    reason = "missing_or_mismatched_mpn"
                elif not ca.spec_sig or not cb.spec_sig:
                    missing_side = "both" if not ca.spec_sig and not cb.spec_sig else ("side_a" if not ca.spec_sig else "side_b")
                    reason = f"spec_sig_empty_{missing_side}"
                elif ca.spec_sig != cb.spec_sig:
                    reason = f"spec_sig_mismatch({ca.spec_sig} vs {cb.spec_sig})"
                elif ca.form != cb.form:
                    reason = f"form_mismatch({ca.form} vs {cb.form})"
                else:
                    reason = "other"
                reasons_split_tp[reason] = reasons_split_tp.get(reason, 0) + 1
            else:
                opt_b_blocked_fp += 1

    total_true = v2_tp
    return {
        "stream_name": "153-Pair Previous Audit Stream",
        "total_pairs": len(items),
        "ground_truth_true_merges": total_true,
        "ground_truth_false_merges": v2_fp,
        "v2_merges": v2_tp + v2_fp,
        "v2_precision": v2_tp / (v2_tp + v2_fp) * 100,
        "v2_recall": v2_tp / total_true * 100,
        "opt_b_merges": opt_b_tp + opt_b_fp,
        "opt_b_tp": opt_b_tp,
        "opt_b_fp": opt_b_fp,
        "opt_b_precision": (opt_b_tp / (opt_b_tp + opt_b_fp) * 100) if (opt_b_tp + opt_b_fp) > 0 else 0.0,
        "opt_b_recall": opt_b_tp / total_true * 100,
        "opt_b_blocked_fp": opt_b_blocked_fp,
        "opt_b_blocked_tp": opt_b_blocked_tp,
        "split_tp_reasons": reasons_split_tp
    }


def sample_and_audit_fresh_200() -> Dict[str, Any]:
    """
    Draws a fresh, deterministic sample of 200 cross-vendor candidate pairs
    from data/components.db and evaluates both Matcher V2 and Option B can_merge().
    """
    random.seed(42)
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Sample cross-vendor candidates by picking high-frequency MPNs and keywords
    candidate_keys = [
        "esp32", "esp8266", "rp2040", "stm32", "arduino", "relay", "servo", "bms",
        "oled", "tft", "dht11", "dht22", "bme280", "bmp280", "mpu6050", "ina219",
        "lm2596", "xl4015", "tp4056", "l298n", "a4988", "drb8825", "ds3231", "hx711",
        "ads1115", "max6675", "ch340", "cp2102", "ft232", "mcp2515", "max485"
    ]

    candidate_pairs: List[Tuple[Any, Any]] = []
    seen_pair_ids = set()

    for key in candidate_keys:
        if len(candidate_pairs) >= 200:
            break
        # Query 15 products from Robu and 15 from other vendors
        cur.execute("""
            SELECT id, vendor, title, price, mpn, sku, category
            FROM products
            WHERE title LIKE ? AND price > 10.0
            LIMIT 40
        """, (f"%{key}%",))
        rows = cur.fetchall()
        
        robu_rows = [r for r in rows if r["vendor"] == "robu"]
        other_rows = [r for r in rows if r["vendor"] != "robu"]

        # Pair them up
        for ra in robu_rows:
            for rb in other_rows:
                pair_id = tuple(sorted([ra["id"], rb["id"]]))
                if pair_id in seen_pair_ids:
                    continue
                seen_pair_ids.add(pair_id)
                candidate_pairs.append((dict(ra), dict(rb)))
                if len(candidate_pairs) >= 200:
                    break
            if len(candidate_pairs) >= 200:
                break

    conn.close()

    # Now evaluate all 200 candidate pairs
    v2_tp = 0
    v2_fp = 0
    v2_tn = 0
    v2_fn = 0

    opt_b_tp = 0
    opt_b_fp = 0
    opt_b_tn = 0
    opt_b_fn = 0

    split_reasons = {}
    audit_samples = []

    for idx, (ra, rb) in enumerate(candidate_pairs):
        ta = ra["title"]
        tb = rb["title"]
        pa = float(ra["price"] or 0.0)
        pb = float(rb["price"] or 0.0)

        is_true_same, gt_reason = evaluate_pair_ground_truth(ta, tb, pa, pb)

        # Matcher V2 evaluation
        m_v2, score_v2, reason_v2 = match_components_v2(ta, tb, pa, pb)

        # Option B evaluation
        ca = ComponentItem(ta, pa, mpn=ra["mpn"])
        cb = ComponentItem(tb, pb, mpn=rb["mpn"])
        m_opt_b = can_merge(ca, cb)

        # Update V2 counts
        if m_v2:
            if is_true_same:
                v2_tp += 1
            else:
                v2_fp += 1
        else:
            if is_true_same:
                v2_fn += 1
            else:
                v2_tn += 1

        # Update Option B counts
        if m_opt_b:
            if is_true_same:
                opt_b_tp += 1
            else:
                opt_b_fp += 1
        else:
            if is_true_same:
                opt_b_fn += 1
                # Why did Option B split this legitimate merge?
                if not ca.mpn or not cb.mpn or ca.mpn != cb.mpn:
                    reason = "missing_or_mismatched_mpn"
                elif not ca.spec_sig or not cb.spec_sig:
                    missing_side = "both" if not ca.spec_sig and not cb.spec_sig else ("side_a" if not ca.spec_sig else "side_b")
                    reason = f"spec_sig_empty_{missing_side}"
                elif ca.spec_sig != cb.spec_sig:
                    reason = f"spec_sig_mismatch({ca.spec_sig} vs {cb.spec_sig})"
                elif ca.form != cb.form:
                    reason = f"form_mismatch({ca.form} vs {cb.form})"
                else:
                    reason = "other"
                split_reasons[reason] = split_reasons.get(reason, 0) + 1
            else:
                opt_b_tn += 1

        if idx < 10:
            audit_samples.append({
                "idx": idx + 1,
                "title_a": ta,
                "vendor_a": ra["vendor"],
                "price_a": pa,
                "title_b": tb,
                "vendor_b": rb["vendor"],
                "price_b": pb,
                "ground_truth_same": is_true_same,
                "gt_reason": gt_reason,
                "v2_verdict": m_v2,
                "v2_reason": reason_v2,
                "opt_b_verdict": m_opt_b,
                "spec_sig_a": ca.spec_sig,
                "spec_sig_b": cb.spec_sig,
                "mpn_a": ca.mpn,
                "mpn_b": cb.mpn
            })

    total_true = v2_tp + v2_fn
    v2_prec = (v2_tp / (v2_tp + v2_fp) * 100) if (v2_tp + v2_fp) > 0 else 0.0
    v2_rec = (v2_tp / total_true * 100) if total_true > 0 else 0.0
    opt_b_prec = (opt_b_tp / (opt_b_tp + opt_b_fp) * 100) if (opt_b_tp + opt_b_fp) > 0 else 0.0
    opt_b_rec = (opt_b_tp / total_true * 100) if total_true > 0 else 0.0

    return {
        "stream_name": "Fresh 200-Pair Cross-Vendor Stream",
        "total_pairs": len(candidate_pairs),
        "ground_truth_true_merges": total_true,
        "ground_truth_non_merges": v2_fp + v2_tn,
        "v2_merges": v2_tp + v2_fp,
        "v2_tp": v2_tp,
        "v2_fp": v2_fp,
        "v2_precision": v2_prec,
        "v2_recall": v2_rec,
        "opt_b_merges": opt_b_tp + opt_b_fp,
        "opt_b_tp": opt_b_tp,
        "opt_b_fp": opt_b_fp,
        "opt_b_precision": opt_b_prec,
        "opt_b_recall": opt_b_rec,
        "opt_b_fn_split": opt_b_fn,
        "split_reasons": split_reasons,
        "samples": audit_samples
    }

if __name__ == "__main__":
    print("=" * 80)
    print("EMPIRICAL OPTION B PRECISION & RECALL AUDIT")
    print("=" * 80)

    # 1. 153 Previous Stream
    res_153 = audit_153_stream()
    if res_153:
        print("\n--- 1. PRIOR MERGE STREAM AUDIT (n=153 merges from Matcher V2) ---")
        print(f"Total Merges in V2 Stream: {res_153['total_pairs']}")
        print(f"Matcher V2 Precision: {res_153['v2_precision']:.2f}% ({res_153['ground_truth_true_merges']} TP, {res_153['ground_truth_false_merges']} FP)")
        print(f"Option B Total Merged: {res_153['opt_b_merges']} (Blocked {res_153['total_pairs'] - res_153['opt_b_merges']})")
        print(f"Option B Precision: {res_153['opt_b_precision']:.2f}% ({res_153['opt_b_tp']} TP, {res_153['opt_b_fp']} FP)")
        print(f"Option B Blocked FPs: {res_153['opt_b_blocked_fp']} / {res_153['ground_truth_false_merges']} ({res_153['opt_b_blocked_fp'] / res_153['ground_truth_false_merges'] * 100:.1f}%)")
        print(f"Option B Recall on True Merges: {res_153['opt_b_recall']:.2f}% ({res_153['opt_b_tp']} / {res_153['ground_truth_true_merges']} preserved)")
        print(f"Legitimate Merges Split into Variants: {res_153['opt_b_blocked_tp']} ({100 - res_153['opt_b_recall']:.1f}%)")
        print("Reasons Legitimate Merges were Split into Variants under Option B:")
        for r, cnt in res_153['split_tp_reasons'].items():
            print(f"  - {r}: {cnt} pairs")

    # 2. Fresh 200 Stream
    res_200 = sample_and_audit_fresh_200()
    print("\n--- 2. FRESH CANDIDATE PAIR AUDIT (n=200 deterministic pairs from live DB) ---")
    print(f"Total Candidate Pairs: {res_200['total_pairs']}")
    print(f"Ground Truth True Merges: {res_200['ground_truth_true_merges']} | True Distinct: {res_200['ground_truth_non_merges']}")
    print(f"\n[Matcher V2 Results]")
    print(f"  Merges Allowed: {res_200['v2_merges']}")
    print(f"  TP: {res_200['v2_tp']} | FP: {res_200['v2_fp']}")
    print(f"  Precision: {res_200['v2_precision']:.2f}%")
    print(f"  Recall:    {res_200['v2_recall']:.2f}%")
    print(f"\n[Option B can_merge() Results]")
    print(f"  Merges Allowed: {res_200['opt_b_merges']}")
    print(f"  TP: {res_200['opt_b_tp']} | FP: {res_200['opt_b_fp']}")
    print(f"  Precision: {res_200['opt_b_precision']:.2f}%")
    print(f"  Recall:    {res_200['opt_b_recall']:.2f}%")
    print(f"  Legitimate Merges Split to Variants (FN): {res_200['opt_b_fn_split']}")
    print("Reasons Legitimate Merges were Split into Variants under Option B:")
    for r, cnt in res_200['split_reasons'].items():
        print(f"  - {r}: {cnt} pairs")
