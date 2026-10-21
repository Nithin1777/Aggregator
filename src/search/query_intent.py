"""
Rule-based query intent classification for electronic component search.
Deterministic, rule-based, no ML.
"""
import csv
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, NamedTuple, Optional

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


def query_vocabulary():
    """Curated catalog words for correction, without treating arbitrary titles as a dictionary."""
    get_query_intent("")
    return dict(_default_classifier.canonical_keys)


def classify_retrieval_tokens(tokens, *, generic_words=(), catalog_brands=()):
    """Identity policies can coexist with separate engineering constraints."""
    generic = set(generic_words)
    vocabulary = query_vocabulary()
    text = " ".join(tokens).lower()
    known_brand = any(kind == "brand" and re.search(r"(?<!\w)" + re.escape(key) + r"(?!\w)", text)
                      for key, kind in vocabulary.items())
    unit_value = re.compile(r"^\d+(?:\.\d+)?(?:v|a|w|rpm|mah|ah|[unpµμ]f|[munµμ]h|kgcm|ohms?)$", re.I)
    model = any(re.search(r"[a-z]", t, re.I) and re.search(r"\d", t) and not unit_value.fullmatch(t) for t in tokens)
    capital_name = any(t.isalpha() and t.isupper() and len(t) >= 3 and t.lower() not in generic for t in tokens)
    identity = known_brand or model or capital_name or bool(catalog_brands)
    phrases = []
    for index, token in enumerate(tokens):
        if token.isdigit() and index:
            phrases.append(tokens[index - 1] + " " + token)
    return {
        "kind": "identity" if identity else "exploratory",
        "catalog_brands": list(catalog_brands),
        # In an identity request modifiers may be part of the product name.
        # Retain all of them; do not maintain a global list of words like Pro.
        "protected_tokens": list(tokens) if identity else [],
        "tokens": list(tokens) if identity else [],
        "phrases": phrases if identity else [],
        "variants": variant_claims(text),
    }


@lru_cache(maxsize=65536)
def identity_pieces(text):
    return tuple(re.findall(r"[a-z]+|\d+(?:\.\d+)?", (text or "").lower()))


def contains_identity_token(text, token):
    pieces, required = identity_pieces(text), identity_pieces(token)
    return bool(required) and any(pieces[i:i + len(required)] == required
                                  for i in range(len(pieces) - len(required) + 1))


def variant_claims(text):
    """Explicit title capacities; several options are ambiguous for one price."""
    return {"capacity_gb": sorted(set(float(m) for m in re.findall(
        r"(?<![\w.])(\d+(?:\.\d+)?)\s*g(?:b|byte)s?\b", text or "", re.I))),
            "lens": sorted(set(re.findall(r"\b(?:wide|standard)\b", (text or "").lower())))}


@lru_cache(maxsize=2048)
def _decoded_policy(payload):
    return json.loads(payload)


def matches_query_identity(title, mpn, sku, payload):
    """SQL eligibility gate, independent of relevance scoring and price ordering."""
    from src.search.product_role import identify_product, sold_title
    policy = _decoded_policy(payload)
    requested_role = policy.get("role", "unknown")
    role = identify_product(title or "").role
    if role in {"accessory", "enclosure", "bundle"} and requested_role != role:
        return 0
    sold = sold_title(title or "")
    if role == requested_role and role in {"accessory", "enclosure", "bundle"}:
        # An explicit accessory request can name its supported product.
        # A request for the product itself cannot use that relationship.
        sold = (title or "").lower()
    fields = [sold, mpn or "", sku or ""]
    if not all(any(contains_identity_token(field, token) for field in fields)
               for token in policy.get("tokens", [])):
        return 0
    if not all(contains_identity_token(sold, phrase) for phrase in policy.get("phrases", [])):
        return 0
    requested_variants = policy.get("variants", {}).get("capacity_gb", [])
    if requested_variants:
        # A base-model MPN cannot establish a RAM/storage option or its price.
        actual = variant_claims(sold)["capacity_gb"]
        if actual != requested_variants:
            return 0
    requested_lens = policy.get("variants", {}).get("lens", [])
    if requested_lens and variant_claims(sold)["lens"] != requested_lens:
        return 0
    return 1


def retrieval_response_fields(contract, count):
    """One response meaning across adapters; alternatives never enter results."""
    if not contract:
        return {}
    identity = bool(contract.get("protected_tokens"))
    status = (("corrected_matches" if contract.get("corrections") else "matches") if count else
              "no_exact_match" if identity else "no_match")
    return {"retrieval_status": status, "identity_preserved": True,
            "results_kind": "confirmed_query_matches", "alternatives": [],
            "query_contract": contract}
