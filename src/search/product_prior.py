"""
Query-to-title affinity used by search ranking.

BM25 and the length penalty answer "how well does this title match the words?".
They do not answer "is this title *about* the thing the user asked for, or is it
an accessory that merely mentions it?". That distinction is the difference
between:

    Dc Motor Mounting Bracket For IG52          (accessory)
    24V 60RPM 100W IG52 ... Geared DC motor     (the part)

Both contain "IG52". This module adds the missing role/position signals:

  * does the query token appear at the start / early, or only after "for ..."?
  * is the title an accessory, enclosure or consumable (bracket, brush, case, ...)?
  * is it a bundle where the accessory is included with the core part?
  * is the title a core component noun (motor, board, sensor, driver, ...)?

The returned affinity is roughly -1.0 .. 1.6. It is deliberately a small
adjustment on top of BM25 + canonical boost, not a replacement for them, so it
does not need a per-query rule for every product family.
"""
from __future__ import annotations

import re
from typing import List, Optional, Set

from normalizer import classify_product_form
from src.spec.taxonomy_loader import get_taxonomy

_STOP_WORDS = frozenset(
    """
    a an the for to of in on at by from with without within into over under
    and or but if then than as is are was were be been being am
    use used using need want can could should would will
    please help find show give tell make get
    """.split()
)

_WORD = re.compile(r"[a-z0-9]+")

# "Surface Mount", "Panel Mount", "DIN mount" describe packaging, not an accessory.
_MOUNT_CONTEXT = re.compile(
    r"\b(surface|panel|pcb|din|wall|chassis|through[- ]hole)\s+mount(?:ing)?\b",
    re.IGNORECASE,
)

# A title that only mentions the query inside a compatibility/attachment clause.
_ATTACHMENT_HINTS = re.compile(
    r"\b(for|with|compatible\s+with|compatible|fits?|fit\s+for|used\s+with|"
    r"suitable\s+for|replacement\s+for|works\s+with|designed\s+for)\b",
    re.IGNORECASE,
)

# Consumables / spares that are never the primary part unless explicitly asked for.
_CONSUMABLE = re.compile(
    r"\b(brushes?|brush|replacement|spares?|pads?|moq|pack\s+of|set\s+of|"
    r"refill|cartridge|blade|nozzle|tip)\b",
    re.IGNORECASE,
)

# Accessory-included-in-the-box ("... with aluminum heat sink", "... + case").
_BUNDLED_ACCESSORY = re.compile(
    r"\b(?:with|\+|includes?|including)\s+(?:an?\s+)?(?:aluminum\s+|steel\s+|metal\s+)?"
    r"(?:heat\s*sink|heatsink|fan|cable|case|cover|holder|bracket|stand|screws?)\b",
    re.IGNORECASE,
)

# Accessories that are not caught by the shared product-form classifier
# (coolers, PSUs, thermal parts are packaging-neutral but still not the part).
_ACCESSORY_ROLE = re.compile(
    r"\b(cooler|cooling\s+fan|fan|heatsink|heat\s*sink|thermal\s+pad|"
    r"psu|power\s+supply|adapter\s+plate|mounting\s+plate|"
    r"enclosure|case|cover|bracket|mount|holder|stand|strap|tie|"
    r"cable|wire|connector|screws?|nuts?|bolts?|standoffs?)\b",
    re.IGNORECASE,
)

# "kit", "combo", "set of" are bundles, not the component itself, unless asked for.
_KIT_ROLE = re.compile(r"\b(kit|combo|set|pack|bundle)\b", re.IGNORECASE)
_KIT_QUERY = re.compile(r"\b(kit|combo|set|pack|bundle|starter)\b", re.IGNORECASE)

# If the user asks for an accessory, the accessory penalty must not fire.
_ACCESSORY_QUERY = re.compile(
    r"\b(brackets?|mount(?:ing)?|holders?|stands?|screws?|brushes?|brush|"
    r"case|cover|enclosure|cable|wire|adapter|connector|heatsink|heat\s*sink|"
    r"fan|cooler|cooling|thermal|psu|power\s+supply|pad|belt|pulley|horn|arm|"
    r"wheel|hub|shaft|coupling|bearing|gearbox|spare|replacement|strap|tie)\b",
    re.IGNORECASE,
)

# Core component nouns: the product category the user is normally looking for.
_COMPONENT_NOUN = re.compile(
    r"\b(motor|motor\s+driver|board|dev(?:elopment)?\s*board|sensor|module|"
    r"driver|relay|servo|battery|cell|display|screen|ic|chip|controller|"
    r"converter|regulator|supply|charger|switch|transistor|mosfet|diode|"
    r"capacitor|resistor|inductor|crystal|antenna|laser|pump|valve|encoder|"
    r"inverter|amplifier|receiver|transmitter|microcontroller|breakout|"
    r"stepper|geared\s+motor|bldc|pcb)\b",
    re.IGNORECASE,
)


def _tokens(text: str) -> Set[str]:
    return set(_WORD.findall((text or "").lower()))


def _token_list(text: str) -> List[str]:
    """Ordered tokens (position signals need the title's word order)."""
    return _WORD.findall((text or "").lower())


def query_tokens(query: str) -> Set[str]:
    return {t for t in _tokens(query) if t not in _STOP_WORDS and len(t) > 1}


def token_matches(query_token: str, title_token: str) -> bool:
    """Prefix-aware match so family queries ('stm32', 'esp32') match part numbers."""
    if query_token == title_token:
        return True
    if len(query_token) >= 3 and len(title_token) > len(query_token):
        return title_token.startswith(query_token)
    return False


def coverage(query: str, title: str) -> float:
    """Fraction of query tokens matched (prefix-aware) anywhere in the title."""
    q_tokens = query_tokens(query)
    if not q_tokens:
        return 0.0
    t_tokens = _tokens(title)
    if not t_tokens:
        return 0.0
    matched = [q for q in q_tokens if any(token_matches(q, t) for t in t_tokens)]
    return len(matched) / len(q_tokens)


def _first_match_index(title_tokens: List[str], q_tokens: Set[str]) -> Optional[int]:
    for index, token in enumerate(title_tokens):
        if any(token_matches(q, token) for q in q_tokens):
            return index
    return None


def query_category_hint(query: str) -> Optional[str]:
    """
    Maps a bare category/domain query ("display", "led", "battery", "raspberry pi")
    to a canonical taxonomy category.

    This is intentionally conservative: accessory queries and ambiguous terms
    return None, so ranking never silently narrows a search the user did not
    narrow. The hint is used as a boost, not a hard filter.
    """
    text = (query or "").strip().lower()
    if not text or _ACCESSORY_QUERY.search(text):
        return None

    candidates: List[tuple[int, str]] = []
    for category in get_taxonomy().categories.values():
        terms = [category.id.replace("-", " "), category.display]
        terms.extend(category.detect_keywords)
        best = 0
        for term in terms:
            normalized = term.strip().lower()
            if not normalized:
                continue
            if text == normalized or text == normalized.rstrip("s"):
                best = max(best, len(normalized) + 10)
            elif re.search(rf"\b{re.escape(normalized)}\b", text):
                best = max(best, len(normalized))
        if best:
            candidates.append((best, category.id))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    if len(candidates) > 1 and candidates[1][0] == candidates[0][0]:
        # "audio amplifier" can legitimately match two categories; don't guess.
        return None
    return candidates[0][1]


def query_affinity(title: str, query: str) -> float:
    """
    Role/position affinity between one title and the user query.

    Positive means "this title is the part"; negative means "this title is an
    accessory or merely mentions the query".
    """
    q_tokens = query_tokens(query)
    if not q_tokens or not title:
        return 0.0

    title_tokens = _token_list(title)
    if not title_tokens:
        return -0.2

    cov = coverage(query, title)
    score = cov
    if cov >= 1.0:
        score += 0.15
    else:
        score -= 0.10 * (1.0 - cov)

    first_idx = _first_match_index(title_tokens, q_tokens)
    if first_idx == 0:
        score += 0.20
    elif first_idx is not None and first_idx <= 2:
        score += 0.10
    elif first_idx is not None and first_idx <= 4:
        score += 0.05

    lower_title = title.lower()
    attachment = _ATTACHMENT_HINTS.search(lower_title)
    if attachment and first_idx is not None:
        # Character position of the first matched title token (approximation is fine).
        match_pos = lower_title.find(title_tokens[first_idx])
        if match_pos > attachment.start():
            score -= 0.30

    if query.strip().lower() in lower_title:
        score += 0.10

    role_title = _MOUNT_CONTEXT.sub(" ", title)
    form = classify_product_form(role_title)
    accessory_query = bool(_ACCESSORY_QUERY.search(query))
    bundled = bool(_BUNDLED_ACCESSORY.search(title))
    consumable = bool(_CONSUMABLE.search(title))
    accessory_role = (
        form in ("accessory", "enclosure")
        or bool(_ACCESSORY_ROLE.search(role_title))
        or consumable
    )
    kit_role = form == "kit" or bool(_KIT_ROLE.search(title))
    kit_query = bool(_KIT_QUERY.search(query))

    if accessory_role and not accessory_query and not bundled:
        score -= 0.55
    if kit_role and not kit_query and not bundled:
        score -= 0.55
    if bundled:
        score += 0.05
    if (
        _COMPONENT_NOUN.search(title)
        and cov >= 0.5
        and not (form in ("accessory", "enclosure") and not bundled)
    ):
        score += 0.10

    return max(-1.0, min(1.6, score))
