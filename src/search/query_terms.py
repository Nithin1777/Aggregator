"""
Shared text-query understanding for catalog search.

Both the web/API retrieval path (`db.search_products`) and the MCP server
(`mcp_server.server`) must treat agent-typed queries the same way:

  "battery to power arduino uno for 8 hours" -> drop stop words, relax AND -> OR
  "esp32 wromm"                              -> correct to "esp32 wroom"
  "mpu650"                                   -> suggest "mpu6050"

This module is pure (no DB access). `db.resolve_query_plan` probes the live
FTS index/vocabulary and produces a `QueryPlan`; the callers then build SQL
with `strict_fts_expr` / `relaxed_fts_expr` / `fts_variations`.
"""
from dataclasses import dataclass, field
import re
from typing import Dict, List, Optional, Sequence, Tuple

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
    return bool(_WORDLIKE.match(token.lower()))


def strict_fts_expr(tokens: Sequence[str]) -> str:
    """All tokens must match (implicit AND), each as a prefix phrase."""
    return " ".join(f'"{t}"*' for t in tokens)


def relaxed_fts_expr(tokens: Sequence[str]) -> str:
    """Any token may match; BM25 `rank` still orders by coverage/relevance."""
    return " OR ".join(f'"{t}"*' for t in tokens)


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


@dataclass
class QueryPlan:
    """Outcome of resolving a user/agent query against the live FTS index."""

    original: str = ""
    tokens: List[str] = field(default_factory=list)
    ignored: List[str] = field(default_factory=list)
    mode: str = "empty"  # empty | strict | corrected | corrected_relaxed | relaxed | unresolved
    effective_tokens: List[str] = field(default_factory=list)
    corrections: Dict[str, str] = field(default_factory=dict)
    suggestions: List[str] = field(default_factory=list)
    warnings: List[Dict[str, object]] = field(default_factory=list)

    @property
    def effective_query(self) -> str:
        return " ".join(self.effective_tokens or self.tokens)

    @property
    def is_relaxed(self) -> bool:
        return self.mode in ("relaxed", "corrected_relaxed")

    @property
    def resolved(self) -> bool:
        return self.mode in ("strict", "corrected", "relaxed", "corrected_relaxed")

    @property
    def simple_expr(self) -> Optional[str]:
        """FTS expression for callers that use one AND/OR expression."""
        if not self.resolved or not self.effective_tokens:
            return None
        if self.is_relaxed:
            return relaxed_fts_expr(self.effective_tokens)
        return strict_fts_expr(self.effective_tokens)
