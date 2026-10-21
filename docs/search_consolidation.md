# Search consolidation

## Shared core

The search core now has explicit ownership boundaries. MCP and web adapters share
filter preparation, eligibility SQL, text planning and relevance scoring. Web
search and browse counts execute the same prepared queries as row retrieval.

| Owner | Responsibility |
| --- | --- |
| `src/search/query_terms.py` | Pure token handling and the text `QueryPlan` model |
| `src/search/planner.py` | Resolve text against catalog vocabulary; preserve identity and requirements; allow bounded generic correction |
| `src/search/filter_plan.py` | Route promoted/vendor fields, validate values, expand equivalent aliases, return structured domain errors |
| `src/spec/constraints.py` | Evidence precedence, ownership, and satisfied/failed/unknown predicates |
| `src/search/catalog_query.py` | Compose one parameterized eligibility relation for rows, counts and diagnostic IDs; SQL ordering policy |
| `src/search/service.py` | Execute prepared local searches on a caller-owned connection; no broadening on execution errors |
| `src/search/ranking.py` | Order eligible candidates and compute specification completeness signals |
| `src/search/retrieval.py` | Count and rank one eligible candidate pool on one read snapshot |
| `db.py` | Persistence and connection lifetime; existing search APIs delegate to the search core |
| `mcp_server/server.py` | Tool arguments, MCP envelopes, diagnostics, pagination and refresh orchestration |

The search core does not import `db`, FastMCP, web routes or vendor adapters.
`retrieval.py` is the compatibility/orchestration boundary that still uses `db`.
Callers own connections; planning does not open a second connection behind them.

## Removed overlapping implementations

- Text-planning implementation in `db.py`. Its public entry point remains a
  compatibility wrapper, and `suggest_query_token` is re-exported.
- Separate web search/count/browse SQL and duplicated LIKE fallback composition.
- Repeated MCP search/count filter routing and validation.
- Separate MCP eligibility assembly and diagnostic count-SQL string rewriting.
- Unused alias/hybrid/vendor SQL compilers in `spec/filters.py`.
- Meilisearch probing that ignored the returned hits and fell through to SQLite.
- The stateless backend class and singleton dispatch around SQLite retrieval.
- Repeated product-row construction in canonical candidate injection.

`build_spec_clauses` remains a small legacy JOIN adapter because an existing
consumer imports its return contract. It delegates individual comparisons to
`_value_conditions`; active retrieval uses `compile_predicates` for full evidence
semantics. Do not use the legacy adapter for a new search path.

## Intended behavioral corrections

- Web specification parameters are bound once, matching their SQL placeholders.
  The former path appended those parameters twice.
- Canonical candidates obey the same category, vendor, stock, price and
  engineering eligibility as ordinary candidates. Ranking cannot bypass filters.
- Web specification requests now share MCP's alias expansion, scope checks and
  vendor-field routing. Previously they used a smaller independent preparation
  path. Invalid or inapplicable filters can therefore produce domain errors
  instead of reaching a different query interpretation.
- Vendor-field coercions are included in shared filter-plan notices.
- Web result ties now include a stable listing-ID order.

## Deliberate boundaries still present

This pass does not establish a single end-to-end request/result contract.

- MCP still owns category inference, query-price extraction, diagnostic response
  construction, fixed-window reranking, cursors and result serialization.
- MCP and web now execute the same required-term expression. Canonical candidate
  injection and automatic any-term/LIKE fallbacks have been removed.
- Local web counts and ranked rows are computed from one prepared request on one
  connection. Live vendor results are rechecked through that eligibility engine.
- Unfiltered web category pages retain broad discovery, including related items;
  MCP defaults to component identity. The web compatibility wrappers explicitly
  select that behavior rather than hiding it in a second SQL compiler.
- Product-role eligibility, listing comparability and ranking affinity have
  different jobs. Query identity and variant checks now share their core
  evidence helpers; some older ranking vocabulary remains separate.
- Identity-preserving correction, contextual engineering constraints and explicit
  preferences now live in the shared plan. See [query policy](query_policy.md).
- `db.py` and the MCP module still contain substantial non-core responsibilities.

Further consolidation should move common result assembly behind adapters. A full identity/variant
evaluation set should guide subsequent behavior changes.

## Validation scope

Source syntax, symbol references and diff whitespace are checked for this pass.
No regression tests or live vendor searches were run as part of consolidation.
Passing test counts in the earlier bug-resolution report describe the code
before this refactor and must not be used as its validation result.

No catalog migration or derived-index rebuild is required for this refactor.
Existing running processes must be restarted to load these source changes.
