"""
Rule-based query intent classification for electronic component search.
Deterministic, rule-based, no ML.
"""
import csv
import re
from pathlib import Path
from typing import Dict, NamedTuple, Optional, Set

# Known model, part, or SKU modifiers indicating a specific part rather than generic brand/category
MODEL_OR_SKU_TOKENS = {
    # Microcontroller / dev board models
    "uno", "nano", "mega", "pro", "mini", "micro", "due", "leonardo", "zero",
    "r3", "r4", "every", "ble", "iot", "rp2040", "pico", "w", "wh",
    "s2", "s3", "c3", "c6", "h2", "wroom", "wrover", "cam", "devkit",
    "4b", "3b", "3b+", "5", "4", "3", "2", "zero2w",
    "f103", "f401", "f411", "nucleo", "discovery", "blackpill", "bluepill",
    # Specific revisions & suffixes
    "v1", "v2", "v3", "v4", "v1.2", "v2.0", "rev", "dip", "smd", "soic",
    "sop", "qfn", "to-220", "type-c", "ch340", "cp2102", "ft232"
}

# General IC / MPN regex pattern
MPN_REGEX = re.compile(r"^[A-Z0-9]{2,}[-_]?[A-Z0-9]{2,}(?:[-_][A-Z0-9]+)*$", re.IGNORECASE)

class QueryIntentResult(NamedTuple):
    intent: str  # 'brand_or_category', 'exact_lookup', 'product_search'
    matched_key: Optional[str] = None
    key_type: Optional[str] = None  # 'brand', 'category', 'mpn'

class QueryIntentClassifier:
    """Classifies search queries into brand_or_category, exact_lookup, or product_search."""

    def __init__(self, seeds_csv_path: Optional[Path] = None):
        if seeds_csv_path is None:
            # Default to seeds/canonical_products.csv relative to project root
            base_dir = Path(__file__).resolve().parent.parent.parent
            seeds_csv_path = base_dir / "seeds" / "canonical_products.csv"
        
        self.canonical_keys: Dict[str, str] = {}  # key -> type ('brand', 'category', 'mpn')
        self._load_seeds(seeds_csv_path)

    def _load_seeds(self, path: Path):
        if not path.exists():
            return
        with open(path, mode="r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                k = row.get("key", "").strip().lower()
                t = row.get("type", "").strip().lower()
                if k:
                    self.canonical_keys[k] = t

    def normalize_query(self, query: str) -> str:
        """Lowercases, normalizes punctuation and trims extra whitespace."""
        q = re.sub(r"[^\w\s-]", " ", query.lower())
        q = re.sub(r"\s+", " ", q).strip()
        return q

    def classify(self, query: str) -> QueryIntentResult:
        clean_q = self.normalize_query(query)
        if not clean_q:
            return QueryIntentResult(intent="product_search")

        tokens = clean_q.split()

        # 1. Check exact key match in canonical_products
        if clean_q in self.canonical_keys:
            ktype = self.canonical_keys[clean_q]
            if ktype == "mpn":
                return QueryIntentResult(intent="exact_lookup", matched_key=clean_q, key_type="mpn")
            elif ktype in ("brand", "category"):
                # Must have no extra model/SKU tokens beyond the key itself
                key_token_set = set(clean_q.split())
                extra_tokens = [t for t in tokens if t not in key_token_set]
                has_model = any(t in MODEL_OR_SKU_TOKENS for t in extra_tokens)
                if not has_model:
                    return QueryIntentResult(intent="brand_or_category", matched_key=clean_q, key_type=ktype)


        # 2. Check if clean_q without hyphens matches an MPN key (e.g. 'hc sr04' -> 'hc-sr04')
        hyphen_candidate = clean_q.replace(" ", "-")
        if hyphen_candidate in self.canonical_keys and self.canonical_keys[hyphen_candidate] == "mpn":
            return QueryIntentResult(intent="exact_lookup", matched_key=hyphen_candidate, key_type="mpn")

        # 3. Check exact_lookup via MPN pattern
        # If single token matches MPN regex (e.g., LM2596, ESP32-S3, STM32F103C8T6)
        if len(tokens) == 1 and bool(re.search(r"\d", tokens[0])):
            if MPN_REGEX.match(tokens[0]):
                return QueryIntentResult(intent="exact_lookup", matched_key=tokens[0], key_type="mpn")

        # 4. Multi-word brand or category matching (e.g. 'raspberry pi')
        for k, ktype in self.canonical_keys.items():
            if ktype in ("brand", "category"):
                # If query exactly equals key
                if clean_q == k:
                    has_model = any(t in MODEL_OR_SKU_TOKENS for t in tokens if t not in k.split())
                    if not has_model:
                        return QueryIntentResult(intent="brand_or_category", matched_key=k, key_type=ktype)

        # 5. Default
        return QueryIntentResult(intent="product_search")


# Singleton instance
_default_classifier: Optional[QueryIntentClassifier] = None

def get_query_intent(query: str) -> QueryIntentResult:
    global _default_classifier
    if _default_classifier is None:
        _default_classifier = QueryIntentClassifier()
    return _default_classifier.classify(query)
