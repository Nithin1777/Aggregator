"""Catalog-aware text planning. Callers own the supplied SQLite connection.

No transport, response-envelope or persistence dependencies belong here.
"""
import difflib
import logging
import re
import sqlite3
from typing import List, Optional

from src.search.query_terms import (QueryPlan, content_tokens, correctable,
                                    detect_unsupported_syntax, exact_fts_expr, parse_price_constraints)
from src.search.requirements import extract_requirements, extract_parametric_requirements
from src.search.query_intent import classify_retrieval_tokens
from src.spec.taxonomy_loader import get_taxonomy, tokenize

logger = logging.getLogger(__name__)


def _catalog_brands(conn, tokens):
    """Bounded indexed probes; catalog brand claims are not spelling suggestions."""
    phrases = [" ".join(tokens[start:start + width]) for start in range(len(tokens))
               for width in range(1, min(3, len(tokens) - start) + 1)]
    spellings = {variant for phrase in phrases
                 for variant in (phrase, phrase.lower(), phrase.upper(), phrase.title())}
    if not spellings:
        return []
    # A pathological long query must not exceed SQLite's parameter limit.
    values = sorted(spellings)[:400]
    try:
        rows = conn.execute(
            "SELECT DISTINCT value FROM product_vendor_specs WHERE key IN ('manufacturer','brand') "
            "AND value IN (" + ",".join("?" for _ in values) + ") LIMIT 10", values,
        ).fetchall()
        return [str(row[0]) for row in rows]
    except sqlite3.OperationalError:
        return []

def _ensure_fts_vocab(conn: sqlite3.Connection) -> bool:
    """
    Lazily creates the FTS5 vocabulary table used for spelling suggestions.

    fts5vocab is a view over the FTS index and costs nothing to keep; creation
    is skipped silently on SQLite builds without the module (results still work,
    only typo suggestions are lost).
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS products_fts_vocab "
            "USING fts5vocab('products_fts', 'row')"
        )
        return True
    except sqlite3.Error as exc:  # noqa: BLE001 - suggestions are best-effort
        logger.debug("FTS vocabulary unavailable (%s); typo suggestions disabled.", exc)
        return False


def _fts_match_count(conn: sqlite3.Connection, expr: str) -> int:
    """Number of indexed rows matching an FTS5 expression."""
    if not expr:
        return 0
    row = conn.execute(
        "SELECT COUNT(1) FROM products_fts WHERE products_fts MATCH ?;", (expr,)
    ).fetchone()
    return int(row[0]) if row else 0


def _vocab_has_term(conn: sqlite3.Connection, term: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM products_fts_vocab WHERE term = ? LIMIT 1;", (term,)
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def suggest_query_token(
    conn: sqlite3.Connection,
    token: str,
    limit: int = 3,
    allow_existing: bool = False,
) -> List[str]:
    """Closest index vocabulary terms to a mistyped word-like token."""
    if not correctable(token):
        return []
    lowered = token.lower()
    if _vocab_has_term(conn, lowered) and not allow_existing:
        return []

    candidates: List[str] = []
    for prefix_len in (3, 2):
        prefix = lowered[:prefix_len]
        if len(prefix) < 2:
            break
        rows = conn.execute(
            "SELECT DISTINCT term FROM products_fts_vocab WHERE term LIKE ? LIMIT 4000;",
            (prefix + "%",),
        ).fetchall()
        candidates = [str(r[0]) for r in rows]
        if len(candidates) >= 5:
            break
    if not candidates:
        return []
    matches = difflib.get_close_matches(lowered, candidates, n=limit + 1, cutoff=0.75)
    return [m for m in matches if m.lower() != lowered][:limit]


def _head_token(tokens: List[str]) -> Optional[str]:
    """
    The earliest token that names a taxonomy category ("battery", "motor", ...).

    The head term supplies category context. All required modifiers remain
    mandatory, whether or not a category can be inferred.
    """
    try:
        taxonomy = get_taxonomy()
        category_tokens = set()
        for category in taxonomy.categories.values():
            category_tokens.add(category.id.replace("-", " "))
            category_tokens.update(category.detect_keywords)
        flat = {token for value in category_tokens for token in tokenize(value)}
        for token in tokens:
            if token in flat:
                return token
            if any(len(term) >= 4 and token.startswith(term) for term in flat):
                return token
    except Exception:  # noqa: BLE001 - head detection is best-effort
        return None
    return None


def _required_expression(tokens):
    """Identifier separator variants preserve complete letter/number groups."""
    expressions = []
    for token in tokens:
        direct = exact_fts_expr([token])
        pieces = re.findall(r"[a-z]+|[0-9]+", token.lower())
        if re.search(r"[a-z]", token, re.I) and re.search(r"[0-9]", token) and len(pieces) > 1:
            expressions.append("(" + direct + ' OR "' + " ".join(pieces) + '")')
        else:
            expressions.append(direct)
    return " AND ".join(expressions)


def resolve_query_plan(query: Optional[str], conn: sqlite3.Connection, category=None) -> QueryPlan:
    """Preserve mandatory intent. No token dropping or automatic any-term search.

    Only missing generic category words may be corrected automatically. Unknown
    identity words produce suggestions, which callers must explicitly resubmit.
    """
    plan = QueryPlan(original=query or "")
    syntax = detect_unsupported_syntax(query)
    if syntax or "\0" in (query or ""):
        plan.mode = "unresolved"
        plan.warnings.append({"code": "unsupported_query_syntax" if syntax else "invalid_query",
                              "message": syntax["message"] if syntax else "NUL is not a valid query character."})
        return plan
    query_text, plan.price_min, plan.price_max = parse_price_constraints(query)
    retrieval, runtime = extract_requirements(query_text)
    plan.unresolved_constraints.extend(runtime)
    taxonomy = get_taxonomy()
    generic = {word.lower() for cat in taxonomy.categories.values()
               for phrase in [cat.id.replace("-", " "), *cat.detect_keywords]
               for word in tokenize(phrase)}
    tokens, ignored = content_tokens(retrieval)
    brands = _catalog_brands(conn, tokens)
    initial = classify_retrieval_tokens(tokens, generic_words=generic, catalog_brands=brands)
    # Never reinterpret a model request merely because its spelling is absent.
    if not initial["protected_tokens"] and _ensure_fts_vocab(conn):
        corrected = []
        for token in tokens:
            candidates = []
            if correctable(token) and not _vocab_has_term(conn, token.lower()):
                candidates = difflib.get_close_matches(token.lower(), sorted(generic), n=2, cutoff=0.8)
            if len(candidates) == 1:
                plan.corrections[token] = candidates[0]
                corrected.append(candidates[0])
            else:
                corrected.append(token)
        if plan.corrections:
            # Apply bounded word replacements before extracting contextual units.
            for source, target in plan.corrections.items():
                retrieval = re.sub(r"(?<!\w)" + re.escape(source) + r"(?!\w)", target, retrieval)
            tokens = corrected
    head = _head_token([t.lower() for t in tokens])
    inferred_category = category
    if not inferred_category and head:
        from src.spec.filters import resolve_category
        inferred_category, _ = resolve_category(head, list(taxonomy.categories))
    from src.search.product_role import identify_product
    requested_role = identify_product(retrieval).role
    if category is None and requested_role in {"accessory", "enclosure", "charger", "controller", "bundle"}:
        # A discovery taxonomy can include related products. Its component-role
        # gate must not override an explicit request for one of those products.
        inferred_category = None
        head = None
    (retrieval, plan.engineering_constraints, plan.predicates, unknown,
     plan.inferred_filters, plan.preference_tokens, plan.inferred_category) = extract_parametric_requirements(
        retrieval, conn, inferred_category,
    )
    plan.unresolved_constraints.extend(unknown)
    if re.search(r"\b(?:without|excluding|must\s+not|not)\b", retrieval, re.I):
        negative = {"kind": "negative_requirement", "evidence": retrieval,
                    "status": "unknown", "reason": "absence_requires_explicit_evidence"}
        unknown.append(negative)
        plan.unresolved_constraints.append(negative)
    if category is None and plan.inferred_category and not conn.execute(
        "SELECT 1 FROM products WHERE category_canonical = ? LIMIT 1", (plan.inferred_category,),
    ).fetchone():
        plan.inferred_category = None
    tokens, plan.ignored = content_tokens(retrieval)
    policy = classify_retrieval_tokens(tokens, generic_words=generic, catalog_brands=brands)
    policy["role"] = identify_product(retrieval).role
    plan.protected_tokens = policy["protected_tokens"]
    plan.identity_policy = policy if plan.protected_tokens else {}
    plan.query_kind = ("identity_parametric" if plan.protected_tokens and plan.predicates else
                       "identity" if plan.protected_tokens else "parametric" if plan.predicates else "exploratory")
    plan.tokens = tokens
    plan.effective_tokens = list(tokens)
    plan.head_token = head or ""
    plan.expression = _required_expression(tokens) or None
    if plan.unresolved_constraints:
        plan.warnings.append({"code": "unresolved_engineering_constraints",
                              "message": "Application requirements are preserved, not verified by text retrieval.",
                              "constraints": plan.unresolved_constraints})
    if plan.preference_tokens:
        plan.warnings.append({"code": "preferences_separate",
                              "message": "Explicit preferences affect ordering, not mandatory eligibility.",
                              "preferences": plan.preference_tokens})
    if unknown:
        plan.mode = "unresolved"
    elif not tokens and (plan.predicates or plan.price_min is not None or plan.price_max is not None):
        plan.mode = "structured"
    elif tokens and _fts_match_count(conn, plan.expression) > 0:
        plan.mode = "corrected" if plan.corrections else "strict"
        if plan.corrections:
            plan.warnings.append({"code": "query_spelling_corrected", "corrections": plan.corrections,
                                  "query_effective": plan.effective_query,
                                  "message": "Corrected missing generic category vocabulary; required terms remain conjoined."})
    else:
        plan.mode = "unresolved"
    if plan.mode == "unresolved":
        if _ensure_fts_vocab(conn):
            for token in tokens:
                for fix in suggest_query_token(conn, token, limit=2):
                    candidate = " ".join(fix if t == token else t for t in tokens)
                    if candidate not in plan.suggestions:
                        plan.suggestions.append(candidate)
        plan.warnings.append({"code": "query_unresolved",
                              "message": "No confirmed match for all required terms. The query was not relaxed; suggestions require a new request.",
                              "protected_tokens": plan.protected_tokens, "suggestions": plan.suggestions})
    return plan
