"""Evidence-backed product identity, separate from discovery taxonomy.

Identity is inferred from entity nouns and attachment relationships, never from
electrical values. Unknown identity remains explicit. Rules are shared by SQL,
response serialization, and specification ownership.
"""
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
import re
import math

import yaml


@lru_cache(maxsize=1)
def _policy():
    path = Path(__file__).resolve().parents[2] / "config" / "product_semantics.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return (
        {k: re.compile(v, re.I) for k, v in data["families"].items()},
        {k: re.compile(v, re.I) for k, v in data["roles"].items()},
        frozenset(data["physical_attributes"]),
        data.get("property_entities", {}),
    )


@dataclass(frozen=True)
class ProductIdentity:
    family: str
    role: str
    evidence: str
    confidence: float
    source: str = "title_relationship_rules_v1"

    def to_dict(self):
        return asdict(self)


@lru_cache(maxsize=65536)
def sold_title(title):
    """The item being sold, excluding supported products and included extras."""
    text = (title or "").lower()
    # An included accessory does not change the identity of the head item.
    head = re.split(r"\b(?:with|includes?|including|featuring)\b|\s\+\s", text, maxsplit=1)[0]
    sold = re.split(r"\b(?:for|compatible\s+with|suitable\s+for|fits?)\b", head, maxsplit=1)[0]
    # Power source and control modifiers name related entities, not the item.
    sold = re.sub(r"\b\w+[ -](?:powered|driven|controlled|operated)\b", " ", sold)
    # Mounting descriptions modify the sold component. They do not name a
    # separately sold mount. Likewise a blade fuse is a fuse, not a fan blade.
    sold = re.sub(r"\b(?:surface|panel|wall|pcb|chassis|flush|through[ -]hole)[ -]mount(?:ed|ing)?\b", " ", sold)
    sold = re.sub(r"\bblade(?=\s+fuses?\b)", " ", sold)
    return sold


@lru_cache(maxsize=65536)
def identify_product(title: str, category: str = "") -> ProductIdentity:
    families, roles, _, _ = _policy()
    sold = sold_title(title)
    matches = [(m.start(), family, m.group()) for family, pat in families.items()
               if (m := pat.search(sold))]
    matches.sort()
    family = matches[0][1] if matches else "unknown"
    evidence = matches[0][2] if matches else ""
    for role, pattern in roles.items():
        if match := pattern.search(sold):
            # Type names here are actual sold entities, not electrical analogies.
            if role == "controller":
                entity = "battery_management" if re.search(r"\bbms\b|battery\s+(?:management|protection)", sold) else "controller"
                return ProductIdentity(entity, role, match.group(), 0.9)
            return ProductIdentity(family, role, match.group(), 0.9)
    if len({m[1] for m in matches}) > 1:
        return ProductIdentity("unknown", "unknown", "Multiple sold-entity families: " + ", ".join(m[1] for m in matches), 0.0)
    # "DC motor fan" is the attachment; a fan assembly explicitly named as
    # such remains its own entity rather than becoming a drive motor.
    if re.search(r"\bfans?\b", sold):
        return ProductIdentity("fan", "component", "fan", 0.85)
    if family != "unknown":
        role = "component"
        if family == "battery":
            role = "cell" if re.search(r"\bcell\b|\b(?:18650|21700)\b", sold) and not re.search(r"\bpack\b", sold) else "pack"
        return ProductIdentity(family, role, evidence, 0.85)
    return ProductIdentity("unknown", "unknown", "No sold-entity noun recognized", 0.0)


def classify_role(title: str, category: str = "") -> str:
    identity = identify_product(title, category)
    if identity.family == "battery_management":
        return "bms"
    if identity.family == identity.role:
        return identity.role
    if identity.family == "battery":
        return {"pack": "battery_pack", "cell": "battery_cell"}.get(identity.role, f"battery_{identity.role}")
    if identity.role == "component":
        return identity.family
    if identity.family != "unknown":
        return f"{identity.family}_{identity.role}"
    return identity.role


def identity_matches(title: str, category: str, requested: str) -> int:
    """Unknown identity cannot confirm membership in a modeled component family."""
    families, _, _, _ = _policy()
    if requested not in families:
        # Unmodeled families retain discovery behavior; never claim verification.
        return 1
    item = identify_product(title, category)
    return int(item.family == requested and item.role in {"component", "pack", "cell"})


@lru_cache(maxsize=131072)
def spec_subject(title: str, category: str, key: str) -> str:
    """Whose property does this attribute describe? Conservative for accessories."""
    if key in _policy()[2]:
        return "sold_item"
    identity = identify_product(title, category)
    from src.spec.vendor_index import VENDOR_KEY_ALIASES
    canonical_key = (VENDOR_KEY_ALIASES.get(category or "") or {}).get(key, key)
    domain = _policy()[3].get(canonical_key)
    if domain:
        if identity.role == "unknown":
            return "unknown"
        if identity.family not in domain["families"] or identity.role not in domain["roles"]:
            return "supported_component"
    if identity.role in {"accessory", "enclosure", "bundle"}:
        return "supported_component"
    return "sold_item"


def register_semantics(conn):
    from src.search.query_intent import matches_query_identity
    from src.spec.vendor_index import vendor_alias_is_current, vendor_fallback_key_applies
    conn.create_function("aggregate_identity_matches", 3, identity_matches, deterministic=True)
    conn.create_function("aggregate_spec_subject", 3, spec_subject, deterministic=True)
    conn.create_function("aggregate_usable_number", 1, usable_number, deterministic=True)
    conn.create_function("aggregate_vendor_alias_valid", 4, vendor_alias_is_current, deterministic=True)
    conn.create_function("aggregate_vendor_fallback_key_applies", 2, vendor_fallback_key_applies, deterministic=True)
    conn.create_function("aggregate_canonical_value", 2, canonical_stored_value, deterministic=True)
    conn.create_function("aggregate_query_identity", 4, matches_query_identity, deterministic=True)


@lru_cache(maxsize=65536)
def canonical_stored_value(value, key):
    """Share normalization of repeated stored values across SQL predicates."""
    from src.spec.filters import canonical_enum_value
    return canonical_enum_value(value, {"key": key})


def usable_number(value):
    try:
        return int(math.isfinite(float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0


def identity_policy_summary():
    families, roles, _, _ = _policy()
    return {"version": 1, "source": "title_relationship_rules_v1",
            "modeled_families": sorted(families), "related_roles": sorted(roles),
            "unknown_policy": "Unknown identity cannot confirm membership in a modeled family",
            "unmodeled_policy": "Broad taxonomy discovery; identity is unverified",
            "classification_is": "Rule evidence, not independent product verification"}
