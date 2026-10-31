# Search semantics in Aggregate 2.3

The [2.4 query policy](query_policy.md) supersedes earlier text correction and
relaxation behavior. Earlier regression results do not validate the new policy.

Search now separates discovery taxonomy, sold-item identity, attribute ownership,
and engineering requirements. These are independent pieces of evidence.

## Shared implementation

Module ownership and the remaining adapter differences are documented in
[search consolidation](search_consolidation.md).

- `config/product_semantics.yaml` defines entity nouns, related roles, and property
  domains independently of vendor categories. It contains no electrical thresholds
  for identifying products.
- `src/search/product_role.py` interprets sold-item nouns and attachment relations,
  preserves evidence/confidence, and abstains on unrecognized or conflicting entities.
- `src/spec/constraints.py` compiles one set of predicates for strict retrieval,
  counting, diagnostics, and candidate discovery. Web/API specification filtering
  uses those predicates too. Unfiltered web category browsing remains discovery.
- `src/search/requirements.py` separates recognized application requirements from
  retrieval words. Both the MCP envelope and web/API `SearchResponse` preserve them.

Identity is evaluated from current titles, so an existing catalog needs no identity
backfill. The bounded in-process cache is keyed by title and category. A changed title
gets a new classification. This avoids a stale parallel identity index.

## Evidence and ownership

`product_identity` includes family, role, evidence, source, and a rule confidence.
These confidence values are heuristic scores, not calibrated probabilities or
independent verification. Electrical units never establish product identity.

`spec_subjects` and `spec_details[*].subject` identify `sold_item`,
`supported_component`, or `unknown`. Properties belonging to a supported component
are exposed as `related_specs` rather than silently flattened into `specs`.
Physical properties such as material may still describe an enclosure itself.
A BMS current rating can describe the BMS without establishing a battery-pack rating.

Vendor attributes remain available in `vendor_specs` and
`available_vendor_spec_keys`, including ordinary discovery without filters. Detailed
responses retain source, confidence, type, unit, and raw-key provenance.
Flat `specs` excludes raw vendor keys that map to a different canonical key;
those aliases remain in the explicit vendor fields with their units. Product
detail and detailed search apply the same rule.

Promoted evidence takes precedence. A conflicting vendor value cannot override it.
Malformed authoritative numeric evidence is unknown and cannot become a strict
match by falling back to a convenient vendor value. Nonfinite numeric values are
unusable. Stored aliases whose meaning no longer matches the alias registry are
excluded from matching and normalized response fields.

Continuous, peak, maximum, and unspecified discharge current remain distinct keys.
New vendor index builds use the corrected mappings. Existing raw attributes remain
available; regenerating `product_vendor_specs` is needed to materialize new aliases.
No source catalog rows are rewritten by the search changes.

## API behavior

For modeled families, a category query requires evidence of the complete component.
Use `include_related=True` on `search_components` or `count_components` to browse the
broader category, including accessories, subsystems, and unknown identities.
`describe_catalog().data.identity_policy` lists modeled families. Unmodeled families
retain taxonomy discovery behavior and must not be interpreted as verified identity.

Each specification constraint has three states:

| State | Meaning |
| --- | --- |
| Satisfied | Usable evidence belonging to the sold item meets the constraint. |
| Failed | Usable evidence belonging to the sold item contradicts it. |
| Unknown | Evidence is absent, malformed, contradictory, or belongs to another entity. |

All strict predicates must be satisfied. Equivalent storage keys can supply the
same requirement, but different caller requirements remain AND constraints.
Equivalent keys that disagree about a constraint make it unknown; the engine does
not select whichever alias happens to pass.

When strict compound results are empty, `include_partial_candidates=True` returns
`confirmed_matches: []` and a separate `partial_candidates` list. Each candidate
has at least one satisfied and one unknown constraint, and no failed constraints.
Candidates are bounded by page size and require verification. They never affect
strict counts or pagination. This is bounded discovery, not an exhaustive relaxed
search and not a claim of suitability.

Runtime phrases expressed in numbers or supported number words, in hours or minutes,
are preserved with their load target and missing inputs. No load or conversion
efficiency is invented. Other requirements remain in `original_query`; a general
engineering-language solver has not been implemented.

## Diagnostic denominators

`describe_spec` reports usable filter populations and accepts the same category,
related-item and vendor-fallback options as search. Its stored extraction count
is separately labeled. Catalog key statistics describe stored rows; their scope
is explicit. Taxonomy entries distinguish default component counts from counts
including related listings. See [the black-box audit remediation](blackbox_bug_resolution.md)
for the 2.3.1 corrections and regression mapping.

`filter_recall` describes the exact text/price/stock/identity scope before specification
constraints. Its disjoint partition is:

```
base_matches = confirmed_matches + filtered_out_for_value + missing_spec
```

An explicit failure takes precedence over missing evidence in this partition.
`any_unknown` separately counts listings with at least one unknown constraint,
including listings that fail another constraint. `per_constraint` reports the three
states and denominator for each requirement. These definitions apply to promoted,
vendor-only, mixed, and alias predicates, including empty scopes.

`promoted_matches` satisfies the entire request with promoted fallback disabled;
explicit vendor filters still apply. `vendor_fallback_matches` is the additional
confirmed population supplied by fallback. `vendor_filled_unknown` measures reduction
in listings with unknown constraints, not extra matches.

Catalog/category coverage describes stored key presence in the broad discovery
population. Category coverage is labeled `scope: discovery_category`. It differs
from the identity-constrained `filter_recall.base_matches`. Page coverage measures
unique serialized sold-item key presence; overlapping aliases are counted once.

`health.spec_backfill_pct` now measures products with at least one promoted spec,
divided by all products. `spec_coverage` gives the numerator and denominator.
`spec_rows_per_product` separately reports extraction density.

## Pagination and validation

SQL ordering includes listing ID as a tie breaker. Text reranking uses fixed
400-listing SQL windows so changing the requested page size cannot change previous
window membership. Cursors bind identity mode and vendor fallback mode. Pagination
is deterministic for an unchanged catalog; offset cursors do not provide a database
snapshot across concurrent catalog edits.

The regression suites include synthetic evidence matrices, cross-family attachment
tests, application requirements, mixed-tier public-tool queries, count/search
agreement, alias composition, and pagination across a ranking-window boundary.
The real-catalog audit cases are retained separately. These checks are not a labeled,
statistically representative precision/recall evaluation.

Run the focused checks with:

```
python -m pytest tests/test_semantic_constraints.py tests/test_semantic_mcp.py tests/test_search_quality_audit.py
```

`scripts/benchmark_search_semantics.py` measures repeated sequential and concurrent
cached calls and checks response invariants. It makes no vendor requests. Refresh
latency and transport reliability need a separate live-server measurement.
