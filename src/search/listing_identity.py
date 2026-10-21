"""Sold form and comparison evidence for identifiers shared by several listings."""
import re
from src.search.product_role import identify_product
from normalizer import extract_pack_quantity


def sold_form(title):
    identity = identify_product(title)
    if identity.role in {"accessory", "enclosure", "bundle", "charger"}:
        return identity.role
    head = re.split(r"\b(?:with|for|compatible|includes?)\b", title, maxsplit=1, flags=re.I)[0]
    if re.search(r"\b(?:modules?|boards?|breakouts?|development\s+kits?)\b", head, re.I):
        return "module"
    return "component" if identity.role in {"component", "pack", "cell"} or re.search(r"\b(?:ic|mosfet|regulators?|timers?|transistors?|diodes?)\b", head, re.I) else "unknown"


def listing_role_conflict(title_a, title_b):
    """Hard evidence that two listings must not become one price comparison."""
    fa, fb = sold_form(title_a), sold_form(title_b)
    if "unknown" not in {fa, fb} and fa != fb:
        return f"sold_form_mismatch:{fa}:{fb}"
    a, b = identify_product(title_a), identify_product(title_b)
    if "unknown" not in {a.family, b.family, a.role, b.role} and (a.family, a.role) != (b.family, b.role):
        return f"sold_identity_mismatch:{a.family}/{a.role}:{b.family}/{b.role}"
    if extract_pack_quantity(title_a) != extract_pack_quantity(title_b):
        return "pack_quantity_mismatch"
    from src.search.query_intent import variant_claims
    from src.search.product_role import sold_title
    va, vb = variant_claims(sold_title(title_a)), variant_claims(sold_title(title_b))
    for key in va:
        if len(va[key]) > 1 or len(vb[key]) > 1:
            return "unresolved_variant_options"
        if bool(va[key]) != bool(vb[key]):
            return f"variant_unverified:{key}"
        if va[key] and vb[key] and va[key] != vb[key]:
            return f"variant_mismatch:{key}"
    return None


def comparison_evidence(anchor, offer):
    """An extracted identifier alone never proves interchangeability."""
    from db import normalize_identifier
    a, b = identify_product(anchor["title"]), identify_product(offer["title"])
    reasons = []
    if not anchor.get("mpn") or normalize_identifier(anchor.get("mpn")) != normalize_identifier(offer.get("mpn")):
        reasons.append("identifier_not_shared")
    fa, fb = sold_form(anchor["title"]), sold_form(offer["title"])
    if "unknown" in {fa, fb} or fa != fb:
        reasons.append("sold_form_unverified_or_different")
    if a.family == "unknown" or b.family == "unknown" or (a.family, a.role) != (b.family, b.role):
        reasons.append("identity_unverified_or_different")
    qa, qb = extract_pack_quantity(anchor["title"]), extract_pack_quantity(offer["title"])
    if qa != qb:
        reasons.append("pack_quantity_differs")
    # Same identifier and broad role still do not establish package, variant,
    # bundle contents or seller authenticity. Keep comparison conservative.
    if normalize_identifier(anchor["title"]) != normalize_identifier(offer["title"]):
        reasons.append("variant_or_package_not_verified")
    return {"price_comparable": not reasons, "comparison_reasons": reasons,
            "grouping_reason": "shared_extracted_identifier_or_canonical_link; identity not guaranteed",
            "sold_form": fb}
