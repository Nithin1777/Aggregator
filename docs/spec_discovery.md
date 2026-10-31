# Specification discovery and enrichment

The shared backend discovers fields from populated curated and normalized vendor
indexes. It does not require adding a field to a category's UI configuration.
Single-observation fields are discoverable; popularity controls ordering, not
whether a field is accessible. Field pages are selected in SQL so a large vendor
vocabulary does not inflate every MCP response.

## MCP workflow

1. `discover_specs(category="microcontrollers", search="adc")` returns fields,
   types, units, supported operators, stored coverage and `filter_parameter`.
2. `spec_values(key="adc_bits", category="microcontrollers", tier="vendor")`
   returns usable values, ranges and missingness. Follow `next_cursor` for more
   values. `search` searches values without truncating the underlying vocabulary.
   Returned counts are exact. For alias fields requiring per-value checks,
   `total_values` can be null: `total_value_candidates` is not a match count.
   Follow `next_cursor` until null, including an empty page. Only the requested
   page is validated, so large vocabularies do not block the first response.
3. `count_components(category="microcontrollers", vendor_filters={"adc_bits":12})`
   sizes the request.
4. `search_components(category="microcontrollers", vendor_filters={"adc_bits":12},
   refresh=False)` retrieves the listings.

`discover_specs` also accepts `tier`, `limit` and `cursor`; its cursor is bound
to category, search, tier and page size. `describe_catalog` and `list_spec_keys`
are overviews and link to this exhaustive discovery path. The former's legacy
top-100 vendor list explicitly discloses its truncation.

Repeated requirements for a field can be expressed with `all`, for example
`{"adc_bits":{"all":[{"gte":12},{"lte":16}]}}`. Requirements supplied through
both automatic field routing and explicit `vendor_filters` are conjoined rather
than allowing either input to overwrite the other. Value inspection retains the
same equivalent storage keys as search/count, including vendor-only fallback
evidence in a category.

Equivalent HTTP endpoints are `/api/specs` and `/api/specs/values`. HTTP search
accepts JSON `vendor_filters` as well as `spec_filters`, including browse mode.
The existing website controls are intentionally deferred at the user's request;
the shared API does not depend on that UI work.

## Query interpretation

Explicitly named numeric properties are interpreted from the current field
registry. This supports arbitrary populated keys, not a per-category property
whitelist. Examples include `microcontroller 12 bit ADC`, `motor shaft diameter
6mm` and `custom field 159 = 7` when that field exists. Named specifications are
resolved before budget extraction and generic spelling correction.

Generic category nouns in a named parametric request become category selectors,
so plural title wording or a title containing only a model does not exclude an
otherwise eligible part. This is disclosed in `category_terms_interpreted` and
`category_scope`. Model/brand requirements are retained. Ambiguous field names
are unresolved and disclose candidate keys rather than guessing a property.
Unrecognized engineering language is not a suitability guarantee.

## Evidence and normalization

Discovery's field counts describe stored rows before ownership/identity checks.
Value discovery and search use the existing three-valued evidence predicates:
missing, contradictory or supported-component evidence cannot confirm a match.

Original attribute values are retained. Explicit ranges produce additional
`<key>_min` and `<key>_max` numeric fields with `vendor_interval` provenance;
the range itself stays text. This includes stated symmetric ppm/degree bounds.
Known unit conversions are applied to endpoints. Unsupported or incompatible
units remain unusable for numeric comparisons. A scraped first number cannot
turn a range into a scalar rating. Vendor specification tables take precedence
over title heuristics in both incremental and full index builds.

## Keeping scrapes indexed

`db.upsert_products` enriches new/changed listings transactionally. It classifies
categories, extracts title and supplied description attributes, normalizes vendor
evidence, derives curated specs and updates affected statistics. Attribute imports
through `replace_product_attributes` publish the same queryable evidence. A
fingerprint avoids repeating enrichment for unchanged price-only updates.

Shopify catalog HTML and Flyrobo product-page description fragments are preserved
for extraction. Source HTML is ingestion-only on `UnifiedProduct`; it is stored
separately and does not bloat search responses. MCP product descriptions remain
readable text. Description enrichment does not silently stop at 80 fields.

## Existing catalogs

Preview without writes or network requests:

```powershell
python scripts/enrich_catalog.py
```

Apply with a SQLite backup before the first write:

```powershell
python scripts/enrich_catalog.py --apply
```

Batches preserve all source listings/descriptions and commit derived evidence
together. `spec_enrichment_state` fingerprints make reruns idempotent. A partial
run can be resumed by rerunning or using `--after-id`; final statistics are rebuilt
once per bulk run rather than rescanning popular fields after every batch.
During the run, field-discovery statistics are not final until publication.

Missing vendor descriptions require targeted collection, not invented values.
The description backfill now supports Flyrobo and indexes successful fetches
without updating prices or listing identities:

```powershell
python scripts/backfill_descriptions.py --vendors flyrobo --limit 100
```

Missing/failed descriptions remain retryable. Existing description-table facts
are retained when only cached plain text is available for offline reprocessing.

## Verification

`tests/test_spec_discovery.py` exercises rare fields beyond 100 entries, scoped
cursors, value pagination, registered MCP serialization/validation, HTTP/MCP
filter parity, arbitrary named properties and model preservation.
`tests/test_spec_enrichment.py` covers ingestion, attribute imports, transactional
rollback, backfill idempotence, evidence priority and scalar/range unit behavior.
All these fixtures use temporary databases. Real-catalog read-only probes and
source-data fingerprints are retained under ignored `reports/backend-quality/`.

## Local catalog validation (2026-10-31)

The complete 129,561-listing catalog was enriched offline after a SQLite backup.
Listing-source and description-source hashes were identical before and after.
Normalized evidence rows increased from 924,038 to 1,112,036. Flyrobo now has
normalized evidence on 1,211 listings from existing titles; missing descriptions
were not invented or counted as collected.

An exhaustive 472-page discovery walk returned all 47,117 populated fields
without missing or duplicate keys. Both normalized and curated indexes have
statistics for every stored key, and SQLite quick_check reports `ok`.

Actual MCP natural-language and structured requests agree: the 12-bit ADC
microcontroller request returns 175 listings, and a 6 mm shaft-diameter motor
request returns nine. These counts reflect catalog claims, not independently
reviewed product suitability. A real stdio handshake advertises all ten tools,
including `discover_specs` and `spec_values`, on server version 2.5.0.

The 215-test backend/evaluator run passed. Subsequent focused checks cover model
identifier protection, category-prefixed vendor labels and schema-declared units.
The final 260-case quality gate reports zero regressions against the unchanged
baseline. Its 41 pre-existing failed checks remain visible; this work does not
certify absolute correctness of the entire search/matching engine.

Reconnect an existing MCP client to reload the new tool catalog. The website
control work remains deferred, as requested; these backend changes do not claim
completion of that frontend phase or collection of every missing vendor page.

The completion audit also checks global alias coverage, conjunctions when an
automatically routed vendor filter shares a key with an explicit requirement,
and curated fields present in storage without a UI definition. Value inspection
validates only the requested candidate page; uncertain alias totals are null,
and callers follow the cursor even when a page contains no usable values.
Category-scoped vendor metadata uses that category's types, units and ranges.

The final isolated release selection passed 210 tests, and the stdio handshake
and both new tool calls passed again. A 64-field real-catalog value round-trip
audit found two signed-number defects; the follow-up confirms both fixes:
`temperature_coefficient_min=-100` advertises and filters 8,942 resistor listings,
and `operating_temperature_min=-40` advertises and filters 6,659 IC listings.
The original audit and separate follow-up report are retained rather than
rewriting the original failure evidence. The frozen quality gate still reports
zero regressions, 31 improvements and the same 41 failed checks.
