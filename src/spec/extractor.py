"""
Generic Taxonomy-Driven Component Classifier and Spec Extractor.
Uses compiled regex patterns and normalization rules from YAML.
No hardcoded categories, attributes, or regex literals.
"""
from dataclasses import dataclass
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
from src.spec.taxonomy_loader import (
    ExtractorDef,
    Taxonomy,
    TaxonomyAttribute,
    TaxonomyCategory,
    get_extractors,
    get_taxonomy,
    run_single_extractor,
)

logger = logging.getLogger(__name__)

@dataclass
class ExtractedSpec:
    key: str
    value: str
    value_num: Optional[float]
    value_type: str
    confidence: float
    evidence: str
    source: str = "rule"

@dataclass
class ExtractionWarning:
    category: str
    key: str
    reason: str
    raw: str

def classify_category(
    title: str,
    taxonomy: Optional[Taxonomy] = None
) -> Optional[Tuple[str, float]]:
    """
    Classifies a product title into a taxonomy category.
    - Word-boundary keyword matching from YAML detect_keywords.
    - confidence = min(1.0, hits / 2.0).
    - Classify if confidence >= 0.4 (1 hit = 0.5, so single-keyword titles DO qualify).
    - If two categories qualify: category with more hits wins.
    - If tied: return None (NULL) and log.
    """
    if not title:
        return None

    if taxonomy is None:
        taxonomy = get_taxonomy()

    qualifying: List[Tuple[str, int, float]] = []

    for cat_id, cat in taxonomy.categories.items():
        if not cat.detect_keywords:
            continue

        # Count keyword hits with word boundaries
        hits = 0
        for kw in cat.detect_keywords:
            kw_str = str(kw).strip()
            if not kw_str:
                continue
            # Word boundary matching
            pat = r"\b" + re.escape(kw_str) + r"\b"
            if re.search(pat, title, re.IGNORECASE):
                hits += 1

        confidence = min(1.0, hits / 2.0)
        if confidence >= 0.4:
            qualifying.append((cat_id, hits, confidence))

    if not qualifying:
        return None

    if len(qualifying) == 1:
        return qualifying[0][0], qualifying[0][2]

    # Multiple categories qualify: sort by hits descending
    qualifying.sort(key=lambda x: x[1], reverse=True)
    top_cat, top_hits, top_conf = qualifying[0]
    runner_up_cat, runner_up_hits, _ = qualifying[1]

    if top_hits == runner_up_hits:
        logger.debug(
            f"Category classification tie between '{top_cat}' and '{runner_up_cat}' "
            f"({top_hits} hits each) for title: {title!r}. Yielding NULL."
        )
        return None

    return top_cat, top_conf

def extract_specs_for_category(
    title: str,
    category_id: str,
    taxonomy: Optional[Taxonomy] = None,
    extractors: Optional[Dict[str, ExtractorDef]] = None
) -> Tuple[Dict[str, ExtractedSpec], List[ExtractionWarning]]:
    """
    Extracts all attributes for a given category from a product title.
    - Follows extractors order in YAML (ORDER = PRECEDENCE).
    - First extractor yielding a valid value wins.
    - Validates enums against values list (logs unmatched_enum warning if not matched).
    - Validates numeric ranges against range [min, max] if defined (logs out_of_range warning).
    """
    if not title or not category_id:
        return {}, []

    if taxonomy is None:
        taxonomy = get_taxonomy()
    if extractors is None:
        extractors = get_extractors()

    category = taxonomy.categories.get(category_id)
    if not category:
        return {}, []

    specs: Dict[str, ExtractedSpec] = {}
    warnings: List[ExtractionWarning] = []

    for attr_key, attr in category.attributes.items():
        if not attr.extract:
            continue

        # Try extractors in listed precedence order
        for ext_id in attr.extract:
            ext_def = extractors.get(ext_id)
            if not ext_def:
                continue

            # Run regex extractor
            match = ext_def.regex.search(title)
            if not match:
                continue

            raw_snippet = match.group(0)
            raw_captured = match.group(1) if match.groups() else raw_snippet
            extracted_val = run_single_extractor(ext_def, title)
            if extracted_val is None:
                continue

            # 1. Enum Validation
            if attr.type == "enum":
                val_str = str(extracted_val).strip()
                if attr.values is not None:
                    # Case-insensitive enum check
                    enum_lookup = {v.lower(): v for v in attr.values}
                    if val_str.lower() in enum_lookup:
                        canonical_val = enum_lookup[val_str.lower()]
                        specs[attr_key] = ExtractedSpec(
                            key=attr_key,
                            value=canonical_val,
                            value_num=None,
                            value_type="enum",
                            confidence=1.0,
                            evidence=raw_snippet,
                            source="rule"
                        )
                        break  # Precedence: first extractor wins
                    else:
                        # Log unmatched_enum warning so enum can be extended in YAML
                        warnings.append(ExtractionWarning(
                            category=category_id,
                            key=attr_key,
                            reason="unmatched_enum",
                            raw=val_str
                        ))
                        # Do not store invalid enum value
                        continue
                else:
                    specs[attr_key] = ExtractedSpec(
                        key=attr_key,
                        value=val_str,
                        value_num=None,
                        value_type="enum",
                        confidence=1.0,
                        evidence=raw_snippet,
                        source="rule"
                    )
                    break

            # 2. Integer / Float Validation
            elif attr.type in ("integer", "float"):
                try:
                    num_val = float(extracted_val)
                except (ValueError, TypeError):
                    continue

                # Range check if defined
                if attr.range:
                    r_min, r_max = attr.range
                    if num_val < r_min or num_val > r_max:
                        warnings.append(ExtractionWarning(
                            category=category_id,
                            key=attr_key,
                            reason="out_of_range",
                            raw=str(extracted_val)
                        ))
                        continue

                val_str = str(int(round(num_val))) if attr.type == "integer" else str(num_val)
                specs[attr_key] = ExtractedSpec(
                    key=attr_key,
                    value=val_str,
                    value_num=num_val,
                    value_type=attr.type,
                    confidence=1.0,
                    evidence=raw_snippet,
                    source="rule"
                )
                break  # Precedence: first extractor wins

            # 3. String Validation
            else:
                val_str = str(extracted_val)
                specs[attr_key] = ExtractedSpec(
                    key=attr_key,
                    value=val_str,
                    value_num=None,
                    value_type="string",
                    confidence=1.0,
                    evidence=raw_snippet,
                    source="rule"
                )
                break

    return specs, warnings
