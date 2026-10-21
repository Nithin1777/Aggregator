# Black-box audit remediation (2.3.1)

The original evidence is retained unchanged in
`BUG_REPORT/AGGREGATE_MCP_BUG_REPORT.md`. Regressions are in
`tests/test_blackbox_bug_report.py`; additional evidence and pagination tests live
in the existing semantic suites.

## Shared causes and changes

| Reports | Cause / resolution |
| --- | --- |
| B01 | Reject NUL queries with `invalid_query`; the shared FTS formatter also removes control characters and quotes terms safely. The regression exercises all 32 ASCII controls through search and count. |
| B02–B03 | An extracted identifier is a relationship between listings, not proof of interchangeability. Exact lookup excludes accessories, cases, chargers and kits by default; mixed component/module identifiers return `mpn_ambiguous` with a sold-form partition. `allow_ambiguous` explicitly opts into broad discovery and is cursor-bound. Offer groups retain individual listing IDs and expose conservative comparison evidence. Unknown identity, different titles/variants, forms or quantities cannot become comparable merely through a shared identifier. |
| B04, B06, B12, B14, B19, B30 | Numeric, categorical and string operands follow typed validation. Lists mean membership; empty operator objects, nulls, booleans and nested operands are errors. Tolerance accepts numbers only. Stored and caller string values share canonicalization, including channel variants and display dimensions. Applied transformations are disclosed. |
| B05, B08, B10 | Taxonomy filter attributes are limited to keys the engine supports. Available values are observed usable promoted values; schema vocabulary remains separately labeled. Component counts and broad discovery counts are explicit; extraction-assignment counts retain their own scope. Supported empty categories are valid queries. |
| B07, B09, B11, B20–B21 | `describe_spec` normalizes the key before selecting a tier and computes coverage, bounded value counts and ranges using retrieval predicates. It accepts category, related-item and vendor-fallback options. Stored-row coverage is separately labeled. Catalog summaries explicitly describe stored coverage and direct callers to `describe_spec` for filterable populations. |
| B13 | The existing shared compiler already conjoins operators. A contradictory equality/range regression now protects that behavior. |
| B15 | The source already rejected unknown explicit vendor keys. A regression protects the error envelope. |
| B16 | Sort order changes ordering only. Zero-price rows are included consistently; they retain their quote/price-basis annotations. Removed the additional sort-dependent title-position filter too. |
| B17 | Source-to-target conversion now uses one dimensional multiplier table. Text already parsed in target units is not converted again; bare numeric strings are in declared units. The derived vendor index can be rebuilt from retained source attributes after a consistent SQLite backup. A category-scoped rebuild preserves unrelated rows and publishes rows/statistics together. |
| B18 | Detailed search and product lookup expose the same promoted/canonical vendor fields and provenance. Raw vendor aliases remain in explicitly named vendor fields rather than introducing duplicate property names into flat `specs`. |
| B22–B23 | A FastMCP boundary validates strict argument types and rejects unknown arguments before SDK validation can throw outside the envelope. Tool schemas forbid additional properties. Invalid arguments return `invalid_arguments`. |
| B24–B25 | Equivalent-key expansion applies inside category scope too. Separate caller requirements remain AND constraints, and per-key counts include the same vendor-fallback and category population. Alias diagnostics materialize atomic truth once, avoiding repeated expansion of vendor evidence scans. |
| B26 | Catalog and health key counts refer to all normalized indexed keys. The discovery listing's minimum count is separately reported. |
| B27, B29 | Partial refresh emits a top-level warning; cached/disabled refresh outcomes remain explicit. Product lookup with refresh disabled returns a non-null `skipped` block. |
| B28 | MPN documentation uses `MY1016ZL`, an identifier represented in this catalog. Existing near-identifier suggestions remain available. |

## Important interpretation details

The same sold-identity/form/quantity veto runs in the normalizer and web comparison
engine before an MPN match or cached merge label can combine listings. MCP offer
annotations are therefore supported by a shared grouping constraint, not only a
response-format correction.

Vendor fallback also checks the category's canonical property mapping. A diode's
raw `voltage` that maps to `reverse_voltage_v` cannot satisfy a generic promoted
voltage filter. Callers can query the precise canonical property or explicitly
inspect the raw vendor field. Only declared equivalent aliases share predicates.

- `specs` values use each key's declared unit. The vendor key `capacitance` has
  historically been normalized to **µF**, not F. Its explicit detail unit is
  authoritative; callers must not infer SI units from an unsuffixed name. Raw
  aliases no longer duplicate canonical flat fields. The report correctly found
  wrong conversions, but its assumption that this field was in F was incorrect.
- Stored extraction coverage can legitimately exceed usable sold-item coverage.
  Several large gaps were nevertheless real classification defects: physical
  descriptors such as “surface mount” and “blade fuse” were read as accessory
  nouns. Those interpretations are corrected.
- Vendor source evidence can still disagree with promoted evidence. Normalization
  cannot establish which underlying vendor claim is correct. Promoted precedence
  and provenance remain visible; this repair does not invent values to reconcile
  contradictory source claims.
- Offer comparability is deliberately conservative and is not a guarantee of
  manufacturer authenticity or full electrical interchangeability.

## Reproduction

```powershell
$env:PRICE_STOCK_REFRESH='0'
.\.venv\Scripts\python.exe -m pytest tests/test_blackbox_bug_report.py tests/test_semantic_constraints.py tests/test_semantic_mcp.py -q
```

Derived-index repair (creates a full consistent backup before modifying the index):

```powershell
.\.venv\Scripts\python.exe scripts/repair_vendor_spec_index.py --backup-dir scratch/catalog_backups
```

## Verification on the repaired catalog

- Core regression suite: **183 passed** (43.77 seconds).
- Grouping and refresh suite: **50 passed** (27.99 seconds), with refresh enabled
  against test fixtures.
- Targeted real-catalog contract checks: **7 passed** (104.35 seconds), covering
  scoped aliases, count/search agreement, payload bounds, and page coverage.
- Detailed search and product lookup agreed on specs and provenance for 100
  catalog listings.
- The derived-index rebuild preserved all **125,508 products**, **808,477 raw
  attribute rows**, **168,226 promoted spec rows**, and **123,299 descriptions**.
  The full pre-repair backup is in `scratch/catalog_backups/`.
- `git diff --check` passed.

These are separate, overlapping suites rather than a count of unique tests.
The test matrix is a regression suite, not a statistically representative product
classification evaluation. Live vendor transport reliability requires separate
end-to-end measurement. Restart an already-running MCP process to load the code.
