"""Guidance delivered to connected clients, including hosts that only load tools."""
import json
from typing import Annotated, Any, Dict, Optional

from pydantic import Field


STRUCTURED_SEARCH_EXAMPLE = {
    "category": "motor",
    "filters": {
        "voltage": "24",
        "rpm": {"gte": 50, "lte": 120},
        "torque_kgcm": {"gte": 80},
    },
    "in_stock_only": True,
    "sort": "price_asc",
    "limit": 20,
}

SERVER_INSTRUCTIONS = """Aggregate searches a catalog of electronic components.
Prefer structured queries for specification-based requests, even when the user
describes the requirements in natural language. Translate explicit requirements
into category, filters/vendor_filters, price_max, in_stock_only and sort. Use
query only for remaining required identity text such as brand, model, MPN or
variant. Omit query when category and filters express the request; do not repeat
the category or encode numeric specifications, stock or price sorting in query.

Workflow:
1. If the category is unknown, use describe_catalog(summary=true). If a field,
unit or operator is unknown, use discover_specs(category=..., search=...). It
includes rare and vendor-only fields; an overview's omission is not absence.
Reuse previously discovered metadata in the same task instead of rediscovering
known keys on every call.
2. Use each field's returned filter_parameter: filters for promoted fields,
vendor_filters for vendor fields. Use spec_values with the same category and
tier when observed values, units or usable coverage need inspection. Follow
next_cursor when needed, including empty pages; missing data is unknown.
3. Encode exact values as scalars, ranges with gte/lte, alternatives with in.
All requested fields are AND requirements. Convert units to the discovered unit;
do not guess keys, silently add tolerances, invent ratings or infer requirements
the user did not give. Retain all required brand/model/variant text in query.
4. For compound constraints or uncertain availability, use count_components to
check feasibility, then search_components with the SAME category, filters,
vendor_filters, query, price_max and in_stock_only. Add sort/limit only to search.
Skip counting if only product rows are needed and the request is already clear.
Use price_asc for cheapest-first and in_stock_only=true when stock is required.
5. If no exact match exists, inspect diagnosis and coverage. Report that result
honestly; do not silently drop constraints or substitute other models. Clearly
label any explicitly explored alternatives and partial candidates. Unknown specs
and vendor claims do not establish application suitability. Check warnings and
price_stock_refresh before claiming live stock or price verification; counts
refer to the cached catalog. Use get_product to inspect shortlisted listings.

Example: 24 V motor, 50-120 RPM, torque at least 80 kg.cm, in stock, cheapest first.
After confirming the field names and units, call search_components with:
""" + json.dumps(STRUCTURED_SEARCH_EXAMPLE, indent=2)

SpecFilters = Annotated[Optional[Dict[str, Any]], Field(
    description=("Primary input for explicit engineering requirements. Discover keys and units with "
                 "discover_specs; use fields whose filter_parameter is filters. Scalar=exact equality; "
                 "gte/lte=range; in=alternatives. Fields are ANDed. Put specs here rather than in query."),
    examples=[STRUCTURED_SEARCH_EXAMPLE["filters"]],
)]
VendorFilters = Annotated[Optional[Dict[str, Any]], Field(
    description=("Structured requirements for fields returned by discover_specs with "
                 "filter_parameter=vendor_filters. Same operators as filters; ANDed with filters."),
    examples=[{"adc_bits": {"gte": 12}}],
)]
IdentityQuery = Annotated[Optional[str], Field(
    description=("Optional required identity text: brand, model, MPN or variant. Omit when category "
                 "and structured filters express the request. Prefer filters/vendor_filters for "
                 "specifications, price_max for budget, and sort for cheapest-first. "
                 "Natural-language parsing is a fallback; identity terms remain required."),
    examples=["STM32F103"],
)]
CatalogCategory = Annotated[Optional[str], Field(
    description="Catalog category, e.g. motor. Discover unknown IDs with describe_catalog(summary=true).",
    examples=["motor"],
)]
