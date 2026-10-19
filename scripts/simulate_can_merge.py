import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from normalizer import (
    ComponentItem,
    match_components_v2,
    extract_spec_signature,
    canonical_base_mpn,
    extract_mpn
)
from scripts.audit_option_b_stream import evaluate_pair_ground_truth

with open('audit_153_parsed.json', 'r', encoding='utf-8') as f:
    items = json.load(f)

def clean_sig(title):
    sig = extract_spec_signature(title)
    if sig == "std_spec":
        return ""
    return sig

def parse_sig_dict(sig_str):
    if not sig_str:
        return {}
    parts = sig_str.split("|")
    d = {}
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            d[k] = v
    return d

def specs_conflict(sig_a, sig_b):
    da = parse_sig_dict(sig_a)
    db = parse_sig_dict(sig_b)
    # Check if there is an overlapping key with differing values
    for k in da:
        if k in db and da[k] != db[k]:
            return True
    return False

# Variant A: Standard rule with empty=''
def new_can_merge_v2(ta, tb, pa=0.0, pb=0.0):
    mpn_a = canonical_base_mpn(extract_mpn(ta))
    mpn_b = canonical_base_mpn(extract_mpn(tb))
    sig_a = clean_sig(ta)
    sig_b = clean_sig(tb)

    # 1. MPN conflict
    if mpn_a and mpn_b and mpn_a != mpn_b:
        return False, "mpn_conflict"

    # 2. Both specs present AND equal
    if sig_a and sig_b and sig_a == sig_b:
        return True, "spec_exact"

    # 3. Both specs present AND differ
    if sig_a and sig_b and sig_a != sig_b:
        return False, "spec_variant"

    # 4. Otherwise (missing spec on either side) -> defer to Tier B
    merged, score, reason = match_components_v2(ta, tb, pa, pb)
    return merged, f"deferred_tier_b:{reason}"

# Variant B: Where differing means actual conflicting dimensions, else asymmetric unknown deferred to V2
def new_can_merge_v3(ta, tb, pa=0.0, pb=0.0):
    mpn_a = canonical_base_mpn(extract_mpn(ta))
    mpn_b = canonical_base_mpn(extract_mpn(tb))
    sig_a = clean_sig(ta)
    sig_b = clean_sig(tb)

    # 1. MPN conflict
    if mpn_a and mpn_b and mpn_a != mpn_b:
        return False, "mpn_conflict"

    # 2. Both specs present AND equal
    if sig_a and sig_b and sig_a == sig_b:
        return True, "spec_exact"

    # 3. Both specs present AND conflict on a shared dimension -> variant family
    if sig_a and sig_b and specs_conflict(sig_a, sig_b):
        return False, "spec_variant"

    # 4. Otherwise (missing spec on either side or asymmetric specs) -> defer to Tier B
    merged, score, reason = match_components_v2(ta, tb, pa, pb)
    return merged, f"deferred_tier_b:{reason}"

print("=== VARIANT A: Literal string equality (empty='') ===")
m_cnt = tp = fp = 0
branches = {}
for it in items:
    ta = it['title_a']; tb = it['title_b']
    pa = float(it.get('price_a', 0.0) or 0.0); pb = float(it.get('price_b', 0.0) or 0.0)
    is_true, _ = evaluate_pair_ground_truth(ta, tb, pa, pb)
    m, r = new_can_merge_v2(ta, tb, pa, pb)
    b_name = r.split(":")[0]
    branches[b_name] = branches.get(b_name, 0) + 1
    if m:
        m_cnt += 1
        if is_true: tp += 1
        else: fp += 1
print(f"Merges: {m_cnt}, TP: {tp}, FP: {fp}, Precision: {tp/m_cnt*100:.2f}%, Recall: {tp/124*100:.2f}%")
print(f"Branches: {branches}\n")

print("=== VARIANT B: Conflicting dimensions in rule 3 ===")
m_cnt = tp = fp = 0
branches = {}
for it in items:
    ta = it['title_a']; tb = it['title_b']
    pa = float(it.get('price_a', 0.0) or 0.0); pb = float(it.get('price_b', 0.0) or 0.0)
    is_true, _ = evaluate_pair_ground_truth(ta, tb, pa, pb)
    m, r = new_can_merge_v3(ta, tb, pa, pb)
    b_name = r.split(":")[0]
    branches[b_name] = branches.get(b_name, 0) + 1
    if m:
        m_cnt += 1
        if is_true: tp += 1
        else: fp += 1
print(f"Merges: {m_cnt}, TP: {tp}, FP: {fp}, Precision: {tp/m_cnt*100:.2f}%, Recall: {tp/124*100:.2f}%")
print(f"Branches: {branches}")
