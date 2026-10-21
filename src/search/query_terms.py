"""
Shared text-query understanding for catalog search.

Both the web/API retrieval path (`db.search_products`) and the MCP server
(`mcp_server.server`) must treat agent-typed queries the same way:

  "battery to power arduino uno for 8 hours" -> preserve runtime; retrieve batteries
  "esp32 wromm"                              -> retain identity; suggest spelling separately
  "LD06"                                     -> require LD06, never LD016

This module is pure (no DB access). `src.search.planner.resolve_query_plan` probes the live
FTS index/vocabulary and produces a `QueryPlan`. Active callers use its exact
expression and eligibility predicates; prefix/relaxation helpers remain for
older internal callers only.
"""
from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Articles, prepositions, auxiliaries and generic filler that agents include in
# natural-language requests. Dropping them lets "the battery for my project"
# match like "battery". Domain words (power, supply, charge, ...) are NOT here.
_STOP_WORDS = frozenset("""
a an the
for to of in on at by from with without within into over under
and or but if then than as is are was were be been being am
i me my we our you your it its this that these those there here
use uses used using need needs needed want wants wanted
can could should would will shall may might must
do does did done doing have has had having
work works worked working compatible compatibility suitable fits fitting
good best better great nice cheap cheapest affordable budget
please help find show give tell make get
what which who whose where when why how
""".split())

# Characters with meaning in FTS5 query syntax; replaced with spaces so user
# text can never alter the generated MATCH expression.
_FTS_UNSAFE = re.compile(r'["\*\(\)\:\^~]')

_ALNUM = re.compile(r"[A-Za-z0-9]")
_WORDLIKE = re.compile(r"^[a-z][a-z0-9]{2,}$")

# FTS5 boolean syntax. The server does not implement a boolean parser, so an
# operator must never be silently folded into an AND/OR search: "esp32 OR
# esp8266" used to become "esp32 esp8266" (11 rows instead of 768).
_BOOLEAN_OPERATOR = re.compile(r"(?<![A-Za-z0-9])(OR|AND|NOT|NEAR)(?![A-Za-z0-9])")
_UNSUPPORTED_CHARS = re.compile(r'["*()]')

_SUPERLATIVE = re.compile(
    r"\b(cheapest|lowest(?:\s+price)?|least\s+expensive|budget|most\s+affordable)\b",
    re.IGNORECASE,
)


def detect_unsupported_syntax(query: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Returns details when the query uses FTS5 boolean/phrase syntax we do not implement.

    Rejecting is deliberate. Honouring `OR`/quotes silently is worse than an
    explicit error because the caller gets a plausible but wrong row count.
    """
    text = query or ""
    operators = [m.group(0) for m in _BOOLEAN_OPERATOR.finditer(text)]
    characters = sorted(set(_UNSUPPORTED_CHARS.findall(text)))
    if not operators and not characters:
        return None
    return {
        "operators": operators,
        "characters": characters,
        "message": (
            "This server does not implement FTS5 boolean/phrase syntax. "
            "Remove OR/AND/NOT/NEAR and quotes, or issue separate searches."
        ),
    }


def detect_sort_intent(query: Optional[str]) -> Optional[str]:
    """Returns 'price_asc' when the query asks for the cheapest/lowest-priced item."""
    if _SUPERLATIVE.search(query or ""):
        return "price_asc"
    return None


def sanitize_query_text(query: Optional[str]) -> str:
    """Removes FTS5 operator characters while keeping part-number punctuation."""
    return _FTS_UNSAFE.sub(" ", query or "").strip()


def raw_tokens(query: Optional[str]) -> List[str]:
    """Whitespace tokens that contain at least one alphanumeric character."""
    return [t for t in sanitize_query_text(query).split() if _ALNUM.search(t)]


def is_stop_word(token: str) -> bool:
    """True for filler words agents wrap around the actual part terms."""
    return re.sub(r"[^a-z0-9]", "", token.lower()) in _STOP_WORDS


def content_tokens(query: Optional[str]) -> Tuple[List[str], List[str]]:
    """
    Splits a query into (search tokens, ignored stop words).

    If a query is nothing but stop words the original tokens are kept so the
    search still behaves predictably instead of matching the whole catalog.
    """
    tokens = raw_tokens(query)
    content = [t for t in tokens if not is_stop_word(t)]
    ignored = [t for t in tokens if is_stop_word(t)]
    if not content:
        return tokens, []
    return content, ignored


def correctable(token: str) -> bool:
    """Only word-like tokens are spell-corrected ("3.7v"/"7ah" are not)."""
    return bool(re.fullmatch(r"[a-z]{3,}", token.lower()))


def _fts_term(token: str) -> str:
    """
    One FTS term. Very short tokens ("4", "5v") are matched exactly.

    Prefix-matching a one-character token ("4"*) turns "4 channel relay" into
    "anything containing 4, 40, 4k, ...", which is how a 1-channel timer relay
    became the top "4 channel relay" result.
    """
    token = re.sub(r"[\x00-\x1f]", " ", token).replace('"', '""')
    if len(token) <= 2:
        return f'"{token}"'
    return f'"{token}"*'


def strict_fts_expr(tokens: Sequence[str]) -> str:
    """All tokens must match (implicit AND), each as a prefix phrase."""
    return " ".join(_fts_term(t) for t in tokens)


def exact_fts_expr(tokens: Sequence[str]) -> str:
    """All complete tokens, with no prefix expansion or user FTS syntax."""
    return " AND ".join('"' + re.sub(r'[\x00-\x1f]', ' ', t).replace('"', '""') + '"'
                        for t in tokens)


def relaxed_fts_expr(tokens: Sequence[str]) -> str:
    """Any token may match; BM25 `rank` still orders by coverage/relevance."""
    return " OR ".join(_fts_term(t) for t in tokens)


def fts_variations(tokens: Sequence[str]) -> List[str]:
    """
    Broader candidate expressions tried together by the web path:
    the strict AND, the concatenated token run, and bigram-merged variants.
    """
    if not tokens:
        return []
    variations = [f"({strict_fts_expr(tokens)})"]
    if len(tokens) > 1:
        concat = "".join(tokens)
        if len(concat) >= 3:
            variations.append(f'("{concat}"*)')
        if len(tokens) > 2:
            for i in range(len(tokens) - 1):
                merged = tokens[i] + tokens[i + 1]
                sub = list(tokens[:i]) + [merged] + list(tokens[i + 2:])
                variations.append(f"({strict_fts_expr(sub)})")
    else:
        # "mpu6050"-style single tokens also match "mpu 6050" titles.
        split = re.match(r"^([a-zA-Z]{2,})(\d+)$", tokens[0])
        if split:
            variations.append(f'("{split.group(1)}"* "{split.group(2)}"*)')

    seen = set()
    deduped = []
    for v in variations:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


# ---------------------------------------------------------------------------
# Price constraints embedded in a text query
# ---------------------------------------------------------------------------

_PRICE_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_PRICE_CURRENCY = r"(?:(?:₹|rs\.?|inr)\s*)?"
_NON_PRICE_UNIT = (
    r"(?:v(?:olts?)?|a(?:mps?)?|w(?:atts?)?|rpm|mah|ah|[unpµμ]f|[munµμ]h|"
    r"[km]?(?:ohms?|Ω)|kg[ .·-]?cm|g(?:b|bytes?)|hours?|hrs?|minutes?|mins?|seconds?|secs?)\b"
)
_PRICE_RANGE_RE = re.compile(
    rf"(?<!\w)(?:₹|rs\.?|inr)\s*{_PRICE_NUM}\s*(?:-|–|to)\s*{_PRICE_CURRENCY}{_PRICE_NUM}(?![\w.,])",
    re.IGNORECASE,
)
_PRICE_MAX_RE = re.compile(
    rf"\b(?:under|below|less\s+than|upto|up\s*to|max(?:imum)?|within)\b\s*"
    rf"{_PRICE_CURRENCY}{_PRICE_NUM}(?![\w.,])(?!(?:\s*){_NON_PRICE_UNIT})",
    re.IGNORECASE,
)
_PRICE_MIN_RE = re.compile(
    rf"\b(?:over|above|more\s+than|at\s+least|min(?:imum)?)\b\s*"
    rf"{_PRICE_CURRENCY}{_PRICE_NUM}(?![\w.,])(?!(?:\s*){_NON_PRICE_UNIT})",
    re.IGNORECASE,
)


def _price_value(raw: str) -> Optional[float]:
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_price_constraints(
    query: Optional[str],
) -> Tuple[str, Optional[float], Optional[float]]:
    """
    Pulls an explicit budget out of free text and returns the remaining query.

    "resistor under ₹500" -> ("resistor", None, 500.0)
    "12v motor ₹300 to 1200" -> ("12v motor", 300.0, 1200.0)
    "esp32 over 300" -> ("esp32", 300.0, None)

    Only explicit comparison words/ranges are treated as prices, so a part
    number like "MAX232" or "ESP32 500" is left untouched.
    """
    text = query or ""
    price_min: Optional[float] = None
    price_max: Optional[float] = None

    match = _PRICE_RANGE_RE.search(text)
    if match:
        low = _price_value(match.group(1))
        high = _price_value(match.group(2))
        if low is not None and high is not None:
            price_min, price_max = min(low, high), max(low, high)
            text = text[:match.start()] + " " + text[match.end():]

    if price_max is None:
        match = _PRICE_MAX_RE.search(text)
        if match:
            price_max = _price_value(match.group(1))
            text = text[:match.start()] + " " + text[match.end():]

    if price_min is None:
        match = _PRICE_MIN_RE.search(text)
        if match:
            price_min = _price_value(match.group(1))
            text = text[:match.start()] + " " + text[match.end():]

    cleaned = re.sub(r"\s+", " ", text).strip(" ,;-")
    return cleaned, price_min, price_max
@dataclass
class QueryPlan:
    """Outcome of resolving a user/agent query against the live FTS index."""

    original: str = ""
    query_kind: str = "exploratory"
    protected_tokens: List[str] = field(default_factory=list)
    identity_policy: Dict[str, Any] = field(default_factory=dict)
    inferred_filters: Dict[str, Any] = field(default_factory=dict)
    predicates: List[Any] = field(default_factory=list)
    engineering_constraints: List[dict] = field(default_factory=list)
    preference_tokens: List[str] = field(default_factory=list)
    expression: Optional[str] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    inferred_category: Optional[str] = None
    unresolved_constraints: List[dict] = field(default_factory=list)
    tokens: List[str] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)
    mode: str = "empty"  # active: empty | strict | structured | corrected | unresolved
    effective_tokens: List[str] = field(default_factory=list)
    corrections: Dict[str, str] = field(default_factory=dict)
    suggestions: List[str] = field(default_factory=list)
    warnings: List[Dict[str, object]] = field(default_factory=list)
    # Legacy compatibility fields. The shared planner never drops required
    # tokens or emits an any-term expression.
    dropped_tokens: List[str] = field(default_factory=list)
    match_mode: str = "all_terms"  # all_terms | any_terms
    operator_tokens: List[str] = field(default_factory=list)
    head_token: str = ""
    head_match_count: int = 0

    @property
    def effective_query(self) -> str:
        return " ".join(self.effective_tokens or self.tokens)

    @property
    def is_relaxed(self) -> bool:
        return self.mode in ("relaxed", "corrected_relaxed")

    @property
    def resolved(self) -> bool:
        return self.mode in ("strict", "structured", "corrected", "relaxed", "corrected_relaxed")

    @property
    def simple_expr(self) -> Optional[str]:
        """FTS expression for callers that use one AND/OR expression."""
        if not self.resolved:
            return None
        if self.expression:
            return self.expression
        if not self.effective_tokens:
            return None
        if self.match_mode == "any_terms":
            return relaxed_fts_expr(self.effective_tokens)
        return exact_fts_expr(self.effective_tokens)

    def contract(self) -> Dict[str, Any]:
        return {
            "policy_version": "identity_preserving_v1", "query_kind": self.query_kind,
            "original_query": self.original, "effective_query": self.effective_query,
            "protected_tokens": self.protected_tokens, "identity_policy": self.identity_policy,
            "identity_preserved": True, "query_relaxed": self.is_relaxed,
            "corrections": self.corrections, "preferences": self.preference_tokens,
            "engineering_constraints": self.engineering_constraints,
            "evidence_scope": "catalog_claims_not_independent_verification",
            "unresolved_constraints": self.unresolved_constraints,
            "price_min": self.price_min, "price_max": self.price_max,
            "suggestions": self.suggestions,
            "match_tier": ("corrected_terms" if self.corrections else
                           "identity_terms" if self.protected_tokens else
                           "structured_constraints" if self.inferred_filters else "all_terms"),
        }
