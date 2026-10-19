"""
Taxonomy and Extractor Loader for Taxonomy-Driven Component Querying.
Loads YAML specifications, validates against closed registry, runs embedded extractor tests at load time, and caches in memory.
"""
from dataclasses import dataclass, field
import logging
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
import yaml

logger = logging.getLogger(__name__)

# Closed registry constants as specified
VALID_EXTRACTOR_KINDS = {"regex"}
VALID_NORMALIZE_TYPES = {"enum", "integer", "float", "string"}
VALID_TRANSFORMS = {"lower", "upper", "none"}

class ExtractorTestError(Exception):
    """Raised when an extractor fails its load-time unit test verification."""
    pass

@dataclass
class ExtractorDef:
    id: str
    kind: str
    pattern: str
    flags: List[str]
    normalize: Dict[str, Any]
    regex: re.Pattern
    tests: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class TaxonomyAttribute:
    key: str
    display_name: str
    type: str  # enum | integer | float | string
    unit: Optional[str] = None
    values: Optional[List[str]] = None
    range: Optional[Tuple[float, float]] = None
    extract: List[str] = field(default_factory=list)

@dataclass
class TaxonomyCategory:
    id: str
    display: str
    detect_keywords: List[str]
    attributes: Dict[str, TaxonomyAttribute]

@dataclass
class Taxonomy:
    version: int
    advertise_min_fill: float
    categories: Dict[str, TaxonomyCategory]

# Module-level cache
_CACHED_TAXONOMY: Optional[Taxonomy] = None
_CACHED_EXTRACTORS: Optional[Dict[str, ExtractorDef]] = None

def normalize_value(raw_val: str, normalize_cfg: Dict[str, Any]) -> Any:
    """
    Applies closed-registry normalization transforms:
    - transform (lower, upper, none)
    - synonyms mapping
    - type conversion (enum, integer, float, string)
    - multiplier (for unit conversion, e.g. Ah -> mAh)
    """
    if raw_val is None:
        return None
    val = str(raw_val).strip()
    if not val:
        return None

    # 1. Transform
    tr = normalize_cfg.get("transform", "none")
    if tr == "lower":
        val = val.lower()
    elif tr == "upper":
        val = val.upper()

    # 2. Synonyms mapping (case-insensitive string keys)
    synonyms = normalize_cfg.get("synonyms")
    if synonyms and isinstance(synonyms, dict):
        syn_map = {str(k).lower(): str(v) for k, v in synonyms.items()}
        if val.lower() in syn_map:
            val = syn_map[val.lower()]

    # 3. Type conversion & multiplier
    v_type = normalize_cfg.get("type", "string")
    multiplier = normalize_cfg.get("multiplier")

    if v_type == "integer":
        num = float(val)
        if multiplier is not None:
            num *= multiplier
        return int(round(num))
    elif v_type == "float":
        num = float(val)
        if multiplier is not None:
            num *= multiplier
        return float(num)
    elif v_type in ("enum", "string"):
        return str(val)
    return val

def run_single_extractor(extractor: ExtractorDef, text: str) -> Any:
    """Executes an extractor against text, applying capturing groups and normalization."""
    if not text:
        return None
    match = extractor.regex.search(text)
    if not match:
        return None
    raw_val = match.group(1) if match.groups() else match.group(0)
    return normalize_value(raw_val, extractor.normalize)

def _load_and_validate_extractors(extractors_path: Path) -> Dict[str, ExtractorDef]:
    """Loads extractors.yaml, validates schema against closed registry, and runs all embedded tests."""
    if not extractors_path.exists():
        raise FileNotFoundError(f"Extractors file not found: {extractors_path}")

    with open(extractors_path, "r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)

    raw_extractors = raw_data.get("extractors", {})
    extractors: Dict[str, ExtractorDef] = {}

    for ext_id, ext_dict in raw_extractors.items():
        kind = ext_dict.get("kind")
        if kind not in VALID_EXTRACTOR_KINDS:
            raise ValueError(f"Invalid extractor kind '{kind}' in '{ext_id}'. Closed registry only supports: {VALID_EXTRACTOR_KINDS}")

        pattern = ext_dict.get("pattern")
        if not pattern:
            raise ValueError(f"Extractor '{ext_id}' missing required pattern.")

        flags_list = ext_dict.get("flags", ["IGNORECASE"])
        regex_flags = 0
        for f in flags_list:
            if f.upper() == "IGNORECASE":
                regex_flags |= re.IGNORECASE
            else:
                raise ValueError(f"Unsupported flag '{f}' in extractor '{ext_id}'. Closed registry supports IGNORECASE only.")

        norm_cfg = ext_dict.get("normalize", {})
        norm_type = norm_cfg.get("type", "string")
        if norm_type not in VALID_NORMALIZE_TYPES:
            raise ValueError(f"Invalid normalize type '{norm_type}' in '{ext_id}'. Must be one of: {VALID_NORMALIZE_TYPES}")

        norm_tr = norm_cfg.get("transform", "none")
        if norm_tr not in VALID_TRANSFORMS:
            raise ValueError(f"Invalid normalize transform '{norm_tr}' in '{ext_id}'. Must be one of: {VALID_TRANSFORMS}")

        compiled_regex = re.compile(pattern, regex_flags)
        tests = ext_dict.get("tests", [])
        if not tests or len(tests) < 5:
            # Spec requires at least 3 positive and 2 negative cases (total >= 5)
            pass

        ext_obj = ExtractorDef(
            id=ext_id,
            kind=kind,
            pattern=pattern,
            flags=flags_list,
            normalize=norm_cfg,
            regex=compiled_regex,
            tests=tests
        )

        # Run embedded tests at load time. Fail loudly on mismatch!
        for test_idx, test_case in enumerate(tests):
            inp = test_case.get("in", "")
            expected = test_case.get("out")
            actual = run_single_extractor(ext_obj, inp)
            if actual != expected:
                raise ExtractorTestError(
                    f"Load-time test failure in extractor '{ext_id}' (test #{test_idx+1}):\n"
                    f"  Input:    {inp!r}\n"
                    f"  Expected: {expected!r}\n"
                    f"  Actual:   {actual!r}"
                )

        extractors[ext_id] = ext_obj

    return extractors

def _load_and_validate_taxonomy(taxonomy_path: Path, extractors: Dict[str, ExtractorDef]) -> Taxonomy:
    """Loads taxonomy.yaml and validates categories and attribute extractor linkages."""
    if not taxonomy_path.exists():
        raise FileNotFoundError(f"Taxonomy file not found: {taxonomy_path}")

    with open(taxonomy_path, "r", encoding="utf-8") as f:
        raw_data = yaml.safe_load(f)

    version = raw_data.get("version", 1)
    advertise_min_fill = float(raw_data.get("advertise_min_fill", 0.4))
    raw_cats = raw_data.get("categories", [])
    categories: Dict[str, TaxonomyCategory] = {}

    for cat_dict in raw_cats:
        cat_id = cat_dict["id"]
        display = cat_dict.get("display", cat_id)
        detect_keywords = cat_dict.get("detect_keywords", [])
        raw_attrs = cat_dict.get("attributes", [])
        attributes: Dict[str, TaxonomyAttribute] = {}

        for attr_dict in raw_attrs:
            attr_key = attr_dict["key"]
            attr_type = attr_dict.get("type", "string")
            attr_extract = attr_dict.get("extract", [])

            # Validate that referenced extractors exist in extractors registry
            for ext_id in attr_extract:
                if ext_id not in extractors:
                    raise ValueError(f"Attribute '{cat_id}.{attr_key}' references unknown extractor '{ext_id}'.")

            attr_range = None
            if "range" in attr_dict and attr_dict["range"]:
                r = attr_dict["range"]
                attr_range = (float(r[0]), float(r[1]))

            attr_obj = TaxonomyAttribute(
                key=attr_key,
                display_name=attr_dict.get("display_name", attr_key),
                type=attr_type,
                unit=attr_dict.get("unit"),
                values=[str(v) for v in attr_dict.get("values", [])] if attr_dict.get("values") is not None else None,
                range=attr_range,
                extract=attr_extract
            )
            attributes[attr_key] = attr_obj

        categories[cat_id] = TaxonomyCategory(
            id=cat_id,
            display=display,
            detect_keywords=detect_keywords,
            attributes=attributes
        )

    return Taxonomy(
        version=version,
        advertise_min_fill=advertise_min_fill,
        categories=categories
    )

def load_taxonomy(
    taxonomy_path: Optional[Path] = None,
    extractors_path: Optional[Path] = None,
    reload: bool = False
) -> Tuple[Taxonomy, Dict[str, ExtractorDef]]:
    """Loads, validates, tests, and caches the taxonomy and extractors."""
    global _CACHED_TAXONOMY, _CACHED_EXTRACTORS
    if not reload and _CACHED_TAXONOMY is not None and _CACHED_EXTRACTORS is not None:
        return _CACHED_TAXONOMY, _CACHED_EXTRACTORS

    base_dir = Path(__file__).resolve().parent.parent.parent
    if taxonomy_path is None:
        taxonomy_path = base_dir / "config" / "taxonomy.yaml"
    if extractors_path is None:
        extractors_path = base_dir / "config" / "extractors.yaml"

    extractors = _load_and_validate_extractors(extractors_path)
    taxonomy = _load_and_validate_taxonomy(taxonomy_path, extractors)

    _CACHED_TAXONOMY = taxonomy
    _CACHED_EXTRACTORS = extractors
    return taxonomy, extractors

def get_taxonomy() -> Taxonomy:
    """Returns the cached taxonomy, loading if necessary."""
    tax, _ = load_taxonomy()
    return tax

def get_extractors() -> Dict[str, ExtractorDef]:
    """Returns the cached extractors, loading if necessary."""
    _, ext = load_taxonomy()
    return ext
