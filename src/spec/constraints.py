"""One three-valued constraint algebra for retrieval, coverage and discovery.

Each predicate has matching and usable-evidence expressions. A failed predicate
has usable evidence that does not match; missing/unusable evidence is unknown.
Promoted evidence takes precedence, including when malformed: vendor data must
not silently replace an authoritative conflicting or unusable value.
"""
from dataclasses import dataclass
from src.spec.filters import _value_conditions, enum_is_numeric


@dataclass(frozen=True)
class Predicate:
    key: str
    match_sql: str
    match_params: tuple
    known_sql: str
    known_params: tuple
    alternatives: tuple = ()

    def state_sql(self):
        # 1 satisfied, 0 unknown, -1 failed. This is the only state definition.
        return (f"CASE WHEN {self.match_sql} THEN 1 WHEN {self.known_sql} THEN -1 ELSE 0 END",
                self.match_params + self.known_params)


def _evidence(key, value, meta, tier, hybrid):
    ownership = "aggregate_spec_subject(p.title, p.category_canonical, ?) = 'sold_item'"
    def evidence_rows(table, alias, matching, fallback=False):
        clauses = [f"{alias}.key = ?"]
        params = [key]
        joins = ""
        if matching:
            conditions, values = _value_conditions(alias, value, meta)
            clauses.extend(conditions)
            params.extend(values)
        if meta.get("type") in {"integer", "float"}:
            clauses.append(f"aggregate_usable_number({alias}.value_num) = 1")
        elif meta.get("type") == "enum" and enum_is_numeric(meta):
            clauses.append(f"aggregate_usable_number({alias}.value) = 1")
        else:
            clauses.append(f"trim({alias}.value) != ''")
        if table == "product_vendor_specs":
            joins = f"JOIN products evidence_owner ON evidence_owner.id = {alias}.product_id"
            clauses.append(f"aggregate_vendor_alias_valid(evidence_owner.category_canonical, {alias}.key, {alias}.raw_key, {alias}.source) = 1")
        if fallback:
            clauses.append(f"aggregate_vendor_fallback_key_applies(evidence_owner.category_canonical, {alias}.key)=1")
            clauses.append(f"NOT EXISTS (SELECT 1 FROM product_specs authoritative WHERE authoritative.product_id = {alias}.product_id AND authoritative.key = ?)")
            params.append(key)
        return f"SELECT {alias}.product_id FROM {table} {alias} {joins} WHERE {' AND '.join(clauses)}", params

    def expression(matching):
        table = "product_vendor_specs" if tier == "vendor" else "product_specs"
        sql, params = evidence_rows(table, "ev", matching)
        if tier != "vendor" and hybrid:
            fallback, values = evidence_rows("product_vendor_specs", "fv", matching, fallback=True)
            sql += " UNION " + fallback
            params += values
        # Candidate IDs come from the spec indexes before title semantics are
        # evaluated. This avoids a correlated probe for every catalog listing.
        return f"(p.id IN ({sql}) AND {ownership})", params + [key]

    matched, match_params = expression(True)
    known, known_params = expression(False)
    return Predicate(key, matched, tuple(match_params), known, tuple(known_params))


def compile_predicates(filters, metadata, *, vendor_filters=None, vendor_metadata=None,
                       hybrid=True, aliases=None):
    predicates = [_evidence(k, v, metadata[k], "promoted", hybrid) for k, v in (filters or {}).items()]
    for key, value in (vendor_filters or {}).items():
        predicates.append(_evidence(key, value, vendor_metadata[key], "vendor", False))
    for group in aliases or []:
        options = [_evidence(k, group["value"], metadata[k], "promoted", hybrid) for k in group["keys"]]
        any_match = "(" + " OR ".join(p.match_sql for p in options) + ")"
        match_params = tuple(v for p in options for v in p.match_params)
        any_known = "(" + " OR ".join(p.known_sql for p in options) + ")"
        known_params = tuple(v for p in options for v in p.known_params)
        any_failed = "(" + " OR ".join(f"({p.known_sql} AND NOT {p.match_sql})" for p in options) + ")"
        failed_params = tuple(v for p in options for v in p.known_params + p.match_params)
        # Equivalent storage keys are evidence for one property. If they
        # disagree about this requirement, its truth is unknown, not whichever
        # alternative happens to make the requested filter pass.
        predicates.append(Predicate(
            group.get("requested_key", group["group"]),
            f"({any_match} AND NOT {any_failed})", match_params + failed_params,
            f"({any_known} AND NOT ({any_match} AND {any_failed}))",
            known_params + match_params + failed_params,
            tuple(options),
        ))
    return predicates


def state_projection(predicates):
    columns, params = [], []
    for index, predicate in enumerate(predicates):
        expression, values = predicate.state_sql()
        columns.append(f"{expression} AS constraint_{index}")
        params.extend(values)
    return ", ".join(columns), params


def individual_match_counts(conn, base_sql, base_params, predicates):
    counts = {}
    for predicate in predicates:
        counts[predicate.key] = int(conn.execute(
            f"SELECT COUNT(*) FROM products p JOIN ({base_sql}) base ON base.id = p.id WHERE {predicate.match_sql}",
            list(base_params) + list(predicate.match_params),
        ).fetchone()[0])
    return counts


def _state_relation(base_sql, base_params, predicates):
    """Materialize atomic truth once before composing aliases.

    Expanding match/known/failure expressions inline repeats each vendor scan
    many times for equivalent keys. Sharing the atomic states avoids that
    expansion while keeping exactly the same three-valued algebra.
    """
    atomic_columns, params, states = [], [], []
    for i, predicate in enumerate(predicates):
        names = []
        for j, atom in enumerate(predicate.alternatives or (predicate,)):
            name = f"atom_{i}_{j}"
            sql, values = atom.state_sql()
            atomic_columns.append(f"{sql} AS {name}")
            params.extend(values)
            names.append(name)
        if len(names) == 1:
            state = names[0]
        else:
            yes = " OR ".join(f"{n}=1" for n in names)
            no = " OR ".join(f"{n}=-1" for n in names)
            state = f"CASE WHEN ({yes}) AND ({no}) THEN 0 WHEN {yes} THEN 1 WHEN {no} THEN -1 ELSE 0 END"
        states.append(f"{state} AS constraint_{i}")
    return ("WITH atomic_states AS MATERIALIZED (SELECT p.id,p.title," + ",".join(atomic_columns) +
            f" FROM products p JOIN ({base_sql}) base ON base.id=p.id), " +
            "states AS MATERIALIZED (SELECT id,title," + ",".join(states) + " FROM atomic_states) ",
            params + list(base_params))


def summarize_states(conn, base_sql, base_params, predicates):
    """Disjoint partition: confirmed, failed (even if others unknown), unknown."""
    if not predicates:
        return None
    relation, params = _state_relation(base_sql, base_params, predicates)
    has_fail = " OR ".join(f"constraint_{i} = -1" for i in range(len(predicates)))
    has_unknown = " OR ".join(f"constraint_{i} = 0" for i in range(len(predicates)))
    per_key_columns = ", ".join(
        f"COALESCE(SUM(constraint_{i} = {state}), 0)"
        for i in range(len(predicates)) for state in (1, -1, 0)
    )
    row = conn.execute(
        relation +
        f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {has_fail} THEN 1 ELSE 0 END), 0), "
        f"COALESCE(SUM(CASE WHEN NOT ({has_fail}) AND ({has_unknown}) THEN 1 ELSE 0 END), 0), "
        f"COALESCE(SUM(CASE WHEN {has_unknown} THEN 1 ELSE 0 END), 0), {per_key_columns} FROM states",
        params,
    ).fetchone()
    base, failed, unknown, any_unknown = map(int, row[:4])
    per_constraint = [{"key": predicate.key, "satisfied": int(row[4 + i * 3]),
                       "failed": int(row[5 + i * 3]), "unknown": int(row[6 + i * 3]),
                       "of": base} for i, predicate in enumerate(predicates)]
    return {"base_matches": base, "confirmed_matches": base - failed - unknown,
            "filtered_out_for_value": failed, "missing_spec": unknown,
            "any_unknown": any_unknown, "keys": [p.key for p in predicates],
            "per_constraint": per_constraint,
            "partition": "base_matches = confirmed_matches + filtered_out_for_value + missing_spec",
            "missing_spec_definition": "No failed constraints and at least one unknown constraint",
            "failed_definition": "At least one usable value explicitly fails; may also have unknown values"}


def discover_candidates(conn, base_sql, base_params, predicates, limit=10):
    if len(predicates) < 2:
        return []
    relation, params = _state_relation(base_sql, base_params, predicates)
    no_fail = " AND ".join(f"constraint_{i} != -1" for i in range(len(predicates)))
    unknown = " OR ".join(f"constraint_{i} = 0" for i in range(len(predicates)))
    score = " + ".join(f"(constraint_{i} = 1)" for i in range(len(predicates)))
    rows = conn.execute(
        relation + f"SELECT *, ({score}) AS satisfied_count "
        f"FROM states WHERE {no_fail} AND ({unknown}) AND ({score}) > 0 "
        "ORDER BY satisfied_count DESC, id ASC LIMIT ?",
        params + [min(50, max(1, limit))],
    ).fetchall()
    return [{"product_id": row["id"], "title": row["title"],
             "satisfied": [p.key for i, p in enumerate(predicates) if row[f"constraint_{i}"] == 1],
             "unknown": [p.key for i, p in enumerate(predicates) if row[f"constraint_{i}"] == 0],
             "failed": [], "requires_verification": True} for row in rows]
