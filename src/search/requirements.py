"""Preserve application requirements independently of disposable retrieval terms."""
import re

_SMALL = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
_NUMBERS = {**dict(zip(_SMALL, range(20))), **_TENS}
_NUMBER_WORDS = r"(?:" + "|".join(_TENS) + r")(?:[ -]+(?:" + "|".join(_SMALL[1:10]) + r"))?|" + "|".join(_SMALL)
_RUNTIME = re.compile(
    r"\b(?:for\s+|runtime\s*(?:of|>=|at least)?\s*)?"
    r"(?P<value>\d+(?:\.\d+)?|" + _NUMBER_WORDS + r")\s*[- ]?\s*"
    r"(?P<unit>hours?|hrs?|minutes?|mins?)\b", re.I)
_LOAD = re.compile(r"\b(?:power|run|operate)\s+(?:(?:an?|the|my)\s+)?(?P<load>(?!for\b).+?)(?=\s+for\b|$)", re.I)


def extract_requirements(query):
    """Recognized constraints retain evidence; the full original query is also kept.

    No load, efficiency, or operating conditions are guessed. Applications that
    we cannot parse remain visible in original_query for the calling agent.
    """
    text = query or ""
    constraints = []
    spans = []
    load = _LOAD.search(text)
    for match in _RUNTIME.finditer(text):
        raw = match["value"].lower()
        value = sum(_NUMBERS[word] for word in re.split(r"[ -]+", raw)) if raw[0].isalpha() else float(raw)
        hours = value / 60 if match["unit"].lower().startswith("min") else value
        constraints.append({"kind": "runtime", "value_hours": hours,
                            "load_target": load["load"].strip() if load else None,
                            "evidence": match.group().strip(), "status": "unknown",
                            "requires": ["load_current_a", "load_voltage_v", "usable_energy_wh", "conversion_efficiency"]})
        spans.append(match.span())
    retrieval = text
    if constraints and load:
        spans.append(load.span())
    merged_spans = []
    for start, end in sorted(spans):
        if merged_spans and start <= merged_spans[-1][1]:
            merged_spans[-1] = (merged_spans[-1][0], max(end, merged_spans[-1][1]))
        else:
            merged_spans.append((start, end))
    for start, end in reversed(merged_spans):
        retrieval = retrieval[:start] + " " + retrieval[end:]
    return re.sub(r"\s+", " ", retrieval).strip(), constraints


_ENGINEERING_VALUE = re.compile(
    r"(?<![\w.])(?:(?P<op>>=|<=|>|<|at\s+least|minimum|min|at\s+most|maximum|max|under|below|above|over|less\s+than|more\s+than)\s*)?"
    r"(?P<number>[+-]?\d+(?:\.\d+)?)(?:\s*[-–]\s*(?P<upper>\d+(?:\.\d+)?))?\s*"
    r"(?P<unit>kg[ .·-]?cm|rpm|mAh|Ah|[unpµμ]F|ohms?|Ω|[kM]Ω|[kM]ohms?|[munµμ]H|V|A)\b", re.I)
_PREFERENCE = re.compile(r"\b(?:preferably|ideally|optionally|preferred)\b\s*[:,-]?", re.I)


def extract_parametric_requirements(query, conn, category=None):
    """Extract only properties whose meaning is established by unit and context.

    Repeated requirements remain separate predicates. Missing schema or ambiguous
    electrical meaning is reported, never guessed from a shared unit.
    """
    from src.spec.filters import load_catalog_specs, parse_and_validate_filters
    from src.spec.constraints import compile_predicates
    from src.spec.filters import build_alias_expansion
    text = query or ""
    preferences = []
    preference = _PREFERENCE.search(text)
    if preference:
        preferences = text[preference.end():].strip(" ,;.").split()
        text = text[:preference.start()].strip(" ,;.")
    matches = list(_ENGINEERING_VALUE.finditer(text))
    inferred_category = category
    if not inferred_category:
        if any(m["unit"].lower().endswith("f") for m in matches):
            inferred_category = "capacitors"
        elif any("ohm" in m["unit"].lower() or "Ω" in m["unit"] for m in matches):
            inferred_category = "resistors"
    metadata = load_catalog_specs(conn) if matches else {}
    constraints, predicates, unknown, spans = [], [], [], []
    filters = {}
    for match in matches:
        unit = match["unit"].lower().replace("μ", "u").replace("µ", "u")
        key = None
        if unit.endswith("f"):
            key = "capacitance_uf"
        elif "ohm" in unit or "Ω" in match["unit"]:
            key = "resistance_ohm"
        elif unit.endswith("h") and unit not in {"ah", "mah"}:
            key = "inductance_uh"
        elif unit in {"ah", "mah"} and inferred_category == "battery":
            key = "capacity_mah"
        elif unit == "rpm" and inferred_category == "motor":
            key = "rpm"
        elif unit.startswith("kg") and inferred_category == "motor":
            key = "torque_kgcm"
        elif unit == "v" and inferred_category in {"motor", "battery", "capacitors", "resistors", "inductors"}:
            key = "voltage" if inferred_category in {"motor", "battery"} else "voltage_v"
        raw = match["number"] + match["unit"]
        operator = re.sub(r"\s+", " ", (match["op"] or "eq").lower())
        operator = {">=": "gte", "<=": "lte", ">": "gt", "<": "lt",
                    "at least": "gte", "minimum": "gte", "min": "gte",
                    "at most": "lte", "maximum": "lte", "max": "lte",
                    "under": "lt", "below": "lt", "above": "gt", "over": "gt",
                    "less than": "lt", "more than": "gt"}.get(operator, operator)
        requirement = ({"gte": raw, "lte": match["upper"] + match["unit"]}
                       if match["upper"] and not match["op"] else {operator: raw})
        normalized, error = parse_and_validate_filters({key: requirement}, metadata) if key in metadata else (None, True)
        if match["upper"] and match["op"]:
            error = True
        if error:
            unknown.append({"kind": "engineering_specification", "evidence": match.group(),
                            "status": "unknown", "reason": "ambiguous_property_or_unavailable_schema",
                            "suggested_key": key})
            continue
        value = normalized[key]
        alias = build_alias_expansion(key, value, metadata)
        predicates.extend(compile_predicates(
            {} if alias else {key: value}, metadata,
            aliases=[{**alias, "requested_key": key}] if alias else None,
        ))
        filters.setdefault(key, []).append(value)
        constraints.append({"key": key, "requirement": value, "evidence": match.group(),
                            "source": "query_unit_and_category", "required": True})
        spans.append(match.span())
    # Packages and dielectric codes are contextual specifications, not model IDs.
    if inferred_category in {"capacitors", "resistors", "inductors"}:
        extra = [(r"\b(?:0201|0402|0603|0805|1206|1210|1812|2010|2512)\b", "package"),
                 (r"\b(?:X7R|X5R|C0G|NP0|Y5V)\b", "dielectric")]
        for pattern, key in extra:
            for match in re.finditer(pattern, text, re.I):
                target_metadata = metadata
                vendor = False
                if key not in target_metadata:
                    from src.spec.vendor_index import load_vendor_specs
                    target_metadata = load_vendor_specs(conn, category=inferred_category, keys=[key], min_count=1)
                    vendor = True
                if key not in target_metadata:
                    continue
                normalized, error = parse_and_validate_filters({key: match.group()}, target_metadata)
                if error:
                    continue
                predicates.extend(compile_predicates(
                    {} if vendor else normalized, metadata,
                    vendor_filters=normalized if vendor else None,
                    vendor_metadata=target_metadata if vendor else None,
                ))
                filters.setdefault(key, []).append(normalized[key])
                constraints.append({"key": key, "requirement": normalized[key], "evidence": match.group(),
                                    "source": "query_unit_and_category", "required": True})
                spans.append(match.span())
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " + text[end:]
    # Property labels do not become unrelated text constraints once represented.
    if constraints:
        labels = {"capacitance_uf": "capacitance", "resistance_ohm": "resistance",
                  "torque_kgcm": "torque", "rpm": "speed", "capacity_mah": "capacity"}
        for key in filters:
            if key in labels:
                text = re.sub(r"\b" + labels[key] + r"\b", " ", text, flags=re.I)
    return (re.sub(r"\s+", " ", text).strip(), constraints, predicates, unknown,
            filters, preferences, inferred_category)
