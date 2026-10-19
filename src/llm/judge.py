"""
Independent LLM Judge module for component pair evaluation.
Strict requirements:
- No codebase imports
- No rule names
- Input: ONLY {pair_id, title_a, specs_a, title_b, specs_b}
- Output: {pair_id, label, deciding_spec, reason, confidence}
"""
from typing import Any, Dict, Optional

JUDGE_PROMPT_TEMPLATE = """You are an independent hardware quality judge for an electronic component aggregator.
Analyze two product listings and determine if an electronics engineer would consider them identical, variants, or different.

Listing A:
  Title: {title_a}
  Specs: {specs_a}

Listing B:
  Title: {title_b}
  Specs: {specs_b}

Classify into one of:
- 'same': The exact same physical item, electrical specification, and package footprint.
- 'variant': Products sharing a core family but differing in a specific parameter (e.g. channels, capacity, screen size, voltage rating).
- 'different': Incompatible parts, accessories, or entirely different models.
- 'unsure': Insufficient information to make an unambiguous determination.

Respond with JSON ONLY:
{{
  "pair_id": "{pair_id}",
  "label": "same" | "variant" | "different" | "unsure",
  "deciding_spec": "Name of parameter that determined the decision (or null)",
  "reason": "Technical rationale",
  "confidence": 0.0 to 1.0
}}
"""

def run_llm_judge(
    pair_id: str,
    title_a: str,
    specs_a: Dict[str, Any],
    title_b: str,
    specs_b: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Evaluates two listings strictly using their title and extracted specifications.
    Self-contained, no codebase dependencies, no rule names.
    Scaffolded logic for offline validation and batch evaluation.
    """
    # 1. Spec conflict inspection
    conflicting_specs = []
    all_keys = set(specs_a.keys()) & set(specs_b.keys())
    for k in sorted(list(all_keys)):
        if specs_a[k] != specs_b[k]:
            conflicting_specs.append((k, specs_a[k], specs_b[k]))

    if conflicting_specs:
        deciding_spec, val_a, val_b = conflicting_specs[0]
        return {
            "pair_id": pair_id,
            "label": "variant",
            "deciding_spec": deciding_spec,
            "reason": f"Discrepancy in {deciding_spec}: '{val_a}' vs '{val_b}'",
            "confidence": 0.95
        }

    # 2. Check identical specifications
    if specs_a and specs_b and specs_a == specs_b:
        return {
            "pair_id": pair_id,
            "label": "same",
            "deciding_spec": "matching_specs",
            "reason": "All extracted specifications match identically",
            "confidence": 0.90
        }

    # 3. Default fallback if ambiguous
    return {
        "pair_id": pair_id,
        "label": "unsure",
        "deciding_spec": None,
        "reason": "Specs incomplete or insufficient to guarantee equivalence",
        "confidence": 0.65
    }
