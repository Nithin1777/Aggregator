"""Translate explicitly named catalog properties into typed requirements.

Names come from the live field registry, not a list of supported motor fields.
Ambiguous labels remain unresolved. This parser never removes a product/model
token or infers electrical suitability from an unqualified number.
"""
import re

from src.spec.discovery import field_metadata
from src.spec.filters import parse_and_validate_filters
from src.spec.constraints import compile_predicates

_OP = r"(?:>=|<=|>|<|=|at\s+least|at\s+most|minimum|maximum|under|over|below|above|exactly)?"
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_VALUE_UNITS = r"[pnuµmkKMG]?(?:V|A|W|F|H|Hz|ohm|m|s)|mm|cm|rpm|bits?|kb|mb|gb|%"
_UNITS = {"v", "a", "w", "ohm", "uf", "uh", "pf", "mhz", "mm", "mm2", "mah", "pct", "kgcm", "bits", "kb", "mb", "gb"}
_OPERATORS = {">=":"gte", "<=":"lte", ">":"gt", "<":"lt", "=":"eq", "at least":"gte",
              "at most":"lte", "minimum":"gte", "maximum":"lte", "under":"lt", "below":"lt",
              "over":"gt", "above":"gt", "exactly":"eq", "":"eq"}


def _phrases(key, meta):
    words = key.split("_")
    unit = words[-1] if words[-1] in _UNITS else None
    core = words[:-1] if unit else words
    phrases = {" ".join(words), key}
    if len(core) >= 2 or unit:
        phrases.add(" ".join(core))
    label = meta.get("display_name", "").casefold()
    if len(label.split()) >= 2:
        phrases.add(label)
    return [(phrase, unit) for phrase in phrases if phrase and len(phrase) >= 3]


def extract_named_requirements(text, conn, category):
    """Return remaining text, disclosed constraints, predicates, and ambiguities."""
    if not re.search(r"(?<!\w)[+-]?(?:\d|\.\d)", text or ""):
        return text, [], [], []
    promoted, vendor = field_metadata(conn, category)
    candidates = {}
    text_words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    for tier, metadata in (("promoted", promoted), ("vendor", vendor)):
        for key, meta in metadata.items():
            if meta["type"] not in {"integer", "float"}:
                continue
            for phrase, unit in _phrases(key, meta):
                identifiers = [word for word in re.findall(r"[a-z0-9]+",phrase.casefold())
                               if re.search(r"[a-z]",word) and re.search(r"\d",word) and word not in _UNITS]
                explicit_key = phrase == key and re.search(rf"(?<!\w){re.escape(key)}\s*(?:=|>=|<=|>|<)",text,re.I)
                if identifiers and not explicit_key:
                    # Vendor labels can contain model names. They cannot turn
                    # an explicit model request into an unrelated field match.
                    continue
                if all(word in text_words for word in re.findall(r"[a-z0-9]+", phrase.casefold())):
                    candidates.setdefault(phrase, []).append((tier, key, meta, unit))
    # Longest explicit names win over substrings such as "current" inside
    # "continuous discharge current". Never truncate the catalog field list.
    if category:
        from src.spec.taxonomy_loader import tokens_equivalent
        category_words = category.replace('-', ' ').split()
        for phrase in list(candidates):
            words = phrase.split()
            if len(words) > 1 and any(tokens_equivalent(words[0], word) for word in category_words):
                # The category noun is context, not an extra property modifier.
                # A rare raw "motor shaft diameter" field must not shadow the
                # shared "shaft diameter" property and exclude other motors.
                if ' '.join(words[1:]) in candidates:
                    del candidates[phrase]
    occupied, constraints, predicates, unknown = [], [], [], []
    for phrase in sorted(candidates, key=lambda value: (-len(value), value)):
        escaped = re.escape(phrase).replace(r"\ ", r"[\s_-]+")
        declared_units = [re.escape(entry[2]['unit']) for entry in candidates[phrase] if entry[2].get('unit')]
        value_pattern = rf"{_NUMBER}(?:[-\s]*(?:{'|'.join(declared_units + [_VALUE_UNITS])}))?"
        patterns = [
            rf"(?<!\w)(?P<name>{escaped})\s*(?:of|is|:)?\s*(?P<op>{_OP})\s*(?P<value>{value_pattern})(?![\w.])",
            rf"(?<!\w)(?P<op>{_OP})\s*(?P<value>{value_pattern})\s+(?P<name>{escaped})(?!\w)",
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.I):
                if any(match.start() < end and match.end() > start for start, end in occupied):
                    continue
                valid = []
                for tier, key, meta, unit in candidates[phrase]:
                    value = match["value"].strip()
                    # Named count/data-size suffixes define the unit even when
                    # vendor metadata stores counts as dimensionless numbers.
                    if unit in {"bits", "kb", "mb", "gb"}:
                        suffix = "bits?" if unit == "bits" else unit
                        value = re.sub(rf"[-\s]*{suffix}$", "", value, flags=re.I)
                    operator = _OPERATORS[re.sub(r"\s+", " ", (match["op"] or "").strip().lower())]
                    normalized, error = parse_and_validate_filters({key:{operator:value}}, {key:meta})
                    if not error:
                        valid.append((tier,key,meta,normalized[key]))
                # A promoted row and its vendor fallback are one field.
                by_key = {}
                for entry in valid:
                    if entry[1] not in by_key or entry[0] == "promoted":
                        by_key[entry[1]] = entry
                valid = list(by_key.values())
                # Prefer an explicitly unit-qualified storage key to its raw
                # counterpart only when the numeric meanings/units agree.
                if len(valid) > 1:
                    qualified = [v for v in valid if any(v[1].endswith("_" + u) for u in _UNITS)]
                    if len(qualified) == 1 and all(v[3] == qualified[0][3] and v[2].get("unit") == qualified[0][2].get("unit") for v in valid):
                        valid = qualified
                if len(valid) != 1:
                    if valid:
                        unknown.append({"kind":"engineering_specification", "evidence":match.group(),
                                        "status":"unknown", "reason":"ambiguous_field_name",
                                        "candidate_keys":[v[1] for v in valid]})
                        occupied.append(match.span())
                    continue
                tier, key, meta, requirement = valid[0]
                predicates.extend(compile_predicates(
                    {key:requirement} if tier == "promoted" else {}, promoted,
                    vendor_filters={key:requirement} if tier == "vendor" else {}, vendor_metadata=vendor,
                ))
                constraints.append({"key":key, "tier":tier, "requirement":requirement,
                                    "evidence":match.group(), "source":"named_catalog_field", "required":True})
                occupied.append(match.span())
    remaining = text
    for start, end in sorted(occupied, reverse=True):
        remaining = remaining[:start] + " " + remaining[end:]
    return re.sub(r"\s+", " ", remaining).strip(), constraints, predicates, unknown
