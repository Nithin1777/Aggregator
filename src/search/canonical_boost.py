"""
Ranking adjustments applied in all modes, weight-gated by intent.
Implements:
1. Canonical Boost: +W_c if product.is_canonical AND product.canonical_for == query_key
2. Length Penalty: score *= 1 / log(1 + token_count) (always on - fixes SEO stuffing)
3. Brand Repetition Penalty: subtract W_r * max(0, count - 1) (always on)
"""
import math
import re
from typing import Any, List, Optional
from src.search.query_intent import QueryIntentResult

class RankingAdjuster:
    """Adjusts search scores using canonical boost, length penalty, and repetition suppression."""

    def __init__(
        self,
        w_canonical_brand_cat: float = 25.0,
        w_canonical_exact: float = 35.0,
        w_canonical_default: float = 2.0,
        w_repetition: float = 4.0
    ):
        self.w_canonical_brand_cat = w_canonical_brand_cat
        self.w_canonical_exact = w_canonical_exact
        self.w_canonical_default = w_canonical_default
        self.w_repetition = w_repetition

    def calculate_adjusted_score(
        self,
        base_score: float,
        title: str,
        is_canonical: bool,
        canonical_for: Optional[str],
        intent_result: QueryIntentResult,
        query: str
    ) -> float:
        """
        Calculates final ranking score for a product given the query and intent.
        Higher score ranks first.
        """
        score = float(base_score)
        norm_title = title.lower()
        title_tokens = re.findall(r"\w+", norm_title)
        token_count = max(1, len(title_tokens))

        # 1. CANONICAL BOOST (+W_c, weight-gated by intent)
        matched_key = intent_result.matched_key or intent_result.normalize_query(query) if hasattr(intent_result, 'normalize_query') else (intent_result.matched_key or query.lower().strip())
        
        is_hero_match = False
        if is_canonical and canonical_for and matched_key:
            canonical_keys = [k.strip().lower() for k in canonical_for.split(",") if k.strip()]
            if matched_key.lower().strip() in canonical_keys:
                is_hero_match = True

        if is_hero_match:
            if intent_result.intent == "brand_or_category":
                w_c = self.w_canonical_brand_cat
            elif intent_result.intent == "exact_lookup":
                w_c = self.w_canonical_exact
            else:
                w_c = self.w_canonical_default
            score += w_c

        # 2. LENGTH PENALTY (always on — score *= 1 / log(1 + token_count))
        # For a short clean title of 4 tokens: log(5) = 1.609 -> multiplier = 0.621
        # For a long SEO title of 25 tokens: log(26) = 3.258 -> multiplier = 0.307
        length_factor = 1.0 / math.log(1.0 + token_count)
        score *= length_factor

        # 3. BRAND / KEYWORD REPETITION PENALTY (always on)
        # Count occurrences of the matched key (or query tokens) in the title
        matched_str = (intent_result.matched_key or query).lower().strip()
        
        # Word-boundary count of the matched key phrase in title
        phrase_matches = len(re.findall(rf"\b{re.escape(matched_str)}\b", norm_title))
        
        if phrase_matches > 1:
            repetition_penalty = self.w_repetition * (phrase_matches - 1)
            score -= repetition_penalty
        elif phrase_matches == 0:
            # If multi-word phrase didn't match contiguously, check individual token repetitions
            check_tokens = [t for t in matched_str.split() if len(t) > 2]
            for tok in check_tokens:
                t_matches = len(re.findall(rf"\b{re.escape(tok)}\b", norm_title))
                if t_matches > 1:
                    score -= self.w_repetition * (t_matches - 1)

        return max(0.01, score)


# Singleton instance
_default_adjuster: Optional[RankingAdjuster] = None

def adjust_search_score(
    base_score: float,
    title: str,
    is_canonical: bool,
    canonical_for: Optional[str],
    intent_result: QueryIntentResult,
    query: str
) -> float:
    global _default_adjuster
    if _default_adjuster is None:
        _default_adjuster = RankingAdjuster()
    return _default_adjuster.calculate_adjusted_score(
        base_score, title, is_canonical, canonical_for, intent_result, query
    )
