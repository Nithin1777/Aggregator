"""
Bucketing logic for triage into confident_merge, confident_reject, or gray.
STRICT PRECEDENCE — do not reorder.
"""
from pathlib import Path
from typing import Optional, Tuple
from normalizer import (
    match_components_v2,
    extract_mpn,
    canonical_base_mpn,
    check_spec_discriminator_conflict,
    extract_spec_discriminators,
)

FAMILY_TOKENS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "family_tokens.txt"
_FAMILY_TOKENS = None


def get_family_tokens() -> set:
    global _FAMILY_TOKENS
    if _FAMILY_TOKENS is None:
        if FAMILY_TOKENS_PATH.exists():
            with open(FAMILY_TOKENS_PATH, "r", encoding="utf-8") as f:
                _FAMILY_TOKENS = {line.strip().upper() for line in f if line.strip() and not line.startswith("#")}
        else:
            _FAMILY_TOKENS = set()
    return _FAMILY_TOKENS


def bucket_pair(
    title_a: str,
    title_b: str,
    price_a: float,
    price_b: float,
    mpn_a: Optional[str] = None,
    mpn_b: Optional[str] = None,
) -> Tuple[str, bool, float, str]:
    """
    Return (bucket, merged, score, reason).
    bucket in {confident_merge, confident_reject, gray}
    STRICT PRECEDENCE — do not reorder.
    """
    merged, score, reason = match_components_v2(
        title_a, title_b, price_a, price_b
    )

    # 1. MPN rescue: same MPN but matcher said no — escalate
    if mpn_a and mpn_b and mpn_a == mpn_b and not merged and score >= 0.5:
        return "gray", merged, score, reason

    # STEP 1: Universal spec guards on Tier A MPN matches
    if merged and ("tier a" in reason.lower() or "mpn match" in reason.lower()):
        has_conflict, conflict_detail = check_spec_discriminator_conflict(title_a, title_b)
        if has_conflict:
            return "gray", False, score, f"tier_a_spec_conflict:{conflict_detail}"

    # STEP 4: Family-MPN check (data-driven from config/family_tokens.txt)
    if merged and ("tier a" in reason.lower() or "mpn match" in reason.lower()):
        cand_mpn = canonical_base_mpn(extract_mpn(title_a))
        family_tokens = get_family_tokens()
        if cand_mpn and cand_mpn in family_tokens:
            s_a = extract_spec_discriminators(title_a)
            s_b = extract_spec_discriminators(title_b)
            # Check if there are distinguishing electrical/hardware specs on both sides that match
            shared_match = False
            for k in ("ram", "voltage", "current", "channels", "chemistry", "screen_size"):
                if k in s_a and k in s_b and s_a[k] == s_b[k]:
                    shared_match = True
                    break
            if not shared_match:
                # No distinguishing specs on either side (both spec-absent or one absent)
                return "gray", merged, score, f"family_mpn_{cand_mpn}_unspecified_specs"

    # STEP 3: Price-ratio check (route price_ratio >= 3.5 to gray, not reject)
    min_p = min(price_a, price_b) if price_a > 0 and price_b > 0 else 0
    max_p = max(price_a, price_b)
    price_ratio = (max_p / min_p) if min_p > 0 else 1.0

    if "extreme price disparity" in reason.lower() or (merged and price_ratio >= 3.5):
        return "gray", merged, score, f"price_ratio_{price_ratio:.1f}"


    # 2. Confident merge
    if merged and score >= 0.85 and "hard veto" not in reason.lower():
        return "confident_merge", merged, score, reason

    # 3. Confident reject
    if not merged and score < 0.55:
        return "confident_reject", merged, score, reason

    # 4. Everything else is gray
    return "gray", merged, score, reason
