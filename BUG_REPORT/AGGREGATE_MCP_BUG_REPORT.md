# Aggregate MCP Server — Bug Report & Fix Plan

**Report date:** 2026-10-21
**Server version:** 2.3.0 (schema 2.3.0)
**DB:** `C:\Users\nithi\Downloads\Platfrom Scrapper\data\components.db`
**Runtime:** Python 3.14.7, pydantic 2.14.1, mcp 1.30.0
**Catalog size:** 125,508 products · 168,226 promoted specs · 924,018 vendor-spec rows

**Method:** black-box testing through the MCP tool interface only (no DB or repo access).
~1,000 calls across all 8 tools: filter operators, type coercion, unit parsing, enum
normalization, category scoping, FTS, pagination, MPN resolution, product detail, and
cross-tool metadata checks. Every defect below was repeated 2–6× and reproduced
deterministically.

**How to use this document:** each item has a repro, observed vs expected, a root-cause
hypothesis, and an acceptance test. Items are ordered by impact. Start with P0.

---

## Summary index

| ID | Severity | Title | Primary tool(s) |
|----|----------|-------|-----------------|
| B01 | P0 | NUL byte in `query` raises an unhandled exception | search, count |
| B02 | P0 | Unrelated products merged under one `product_id` | get_product, search_by_mpn |
| B03 | P0 | `search_by_mpn` returns accessories/modules as the part | search_by_mpn |
| B04 | P0 | `resolution` and `thread_size` are completely unfilterable | count, search, describe_spec |
| B05 | P0 | Three advertised filter keys do not exist | describe_catalog → filters |
| B06 | P0 | Bare list values are stringified → silent 0 matches | count, search |
| B07 | P1 | Declared spec coverage unreachable for 44/49 keys | describe_spec vs filters |
| B08 | P1 | Three different product counts per category | describe_catalog vs category |
| B09 | P1 | `describe_spec` case/space silently reads vendor layer | describe_spec |
| B10 | P1 | 32/175 advertised per-category enum values are dead | describe_catalog vs filters |
| B11 | P1 | `describe_spec.values` counts disagree with filtering (101/110) | describe_spec vs filters |
| B12 | P1 | `channel_type` stores un-normalized variants | filters, describe_spec |
| B13 | P1 | `eq` silently overrides `gte`/`lte` | filters |
| B14 | P1 | Empty operator dict `{}` has undocumented, type-dependent semantics | filters |
| B15 | P1 | Unknown `vendor_filters` key returns silent 0 | filters |
| B16 | P1 | `sort` changes `total_matches` (price-zero exclusion) | search |
| B17 | P1 | Capacitor `capacitance` alias is off by 10⁶ | search rows |
| B18 | P1 | `specs` shape differs between search and detail | search vs get_product |
| B19 | P1 | Boolean `tolerance` coerced to ±100% | filters |
| B20 | P1 | `resistance_ohm` declared max unreachable | describe_spec vs filters |
| B21 | P1 | `describe_spec(vendor=true)` returns no values/range | describe_spec |
| B22 | P2 | Schema-validation errors bypass structured envelope | all tools |
| B23 | P2 | Unknown top-level arguments silently ignored | search, count |
| B24 | P2 | Alias expansion disabled when `category` is present | filters |
| B25 | P2 | `filter_expanded.per_key_counts` does not reconcile | filters |
| B26 | P2 | Vendor-spec key count contradicts `health` | describe_catalog, health |
| B27 | P2 | Partial live refresh reported without a warning | search, get_product |
| B28 | P2 | Documented MPN example returns nothing | search_by_mpn |
| B29 | P2 | `get_product` omits refresh status when `refresh:false` | get_product |
| B30 | P2 | `filter_value_coerced` is incomplete | filters |

---

## P0 — crashes and silently wrong result sets

### B01 — NUL byte in `query` raises an unhandled exception

**Severity:** P0 (crash)
**Tools:** `search_components`, `count_components`

**Repro**

```js
search_components({ query: "resistor\u0000motor" })
count_components({ query: "a\u0000b" })
```

**Observed**

```
Error: Error executing tool search_components: unterminated string
Error: Error executing tool count_components: unterminated string
```

8/8 reproductions. The response is a raw Python exception string, not the standard
envelope (`ok:false`, `error_code`, `message`). The same NUL in `mpn`
(`search_by_mpn`) and in filter values (`filters:{package:"0603\u0000"}`) is handled
safely, so this is specific to the FTS/query path.

**Expected**

Either `ok:true` with the NUL stripped/sanitized, or `ok:false` with
`error_code:"invalid_query"` and a normal message.

**Root-cause hypothesis**

The query string is interpolated into an FTS5 `MATCH` expression. NUL truncates the
underlying C string during SQLite statement binding, leaving an unterminated quote or
token, which surfaces as a `sqlite3` / wrapper exception outside the tool's error
handler.

**Acceptance test**

- `query` containing `\u0000` at start/middle/end returns a structured response, never
  an exception.
- Add a fuzz case to the test suite: every control character `\x00`–`\x1f` in `query`.

---

### B02 — Unrelated products merged under one `product_id`

**Severity:** P0 (data integrity / misleading prices)
**Tools:** `get_product`, `search_by_mpn`, `search_components`

**Repro**

```js
get_product({ product_id: 44767 })
search_by_mpn({ mpn: "NE555" })
```

**Observed**

One product record with **29 `vendor_offers`, 27 distinct titles**, all
`price_comparable: true`, `price_basis: "per_unit"`, price range **4 → 1,109 INR**:

- `NE555DR SOIC-8 Timer`
- `NE555 Precision Timer IC` (bare IC, 4 INR)
- `NE555 Adjustable Frequency Pulse Generator Module`
- `NE555 Delay Timer Switch Adjustable Relay Module`
- `NE555 + CD4017 Water Flowing Light LED Module DIY Kit` (79 INR)
- … up to 1,109 INR

`product_identity` is `{family:"unknown", role:"unknown", evidence:"No sold-entity noun
recognized", confidence:0}`.

**Why it matters**

The product is presented as a set of comparable vendor offers. A consumer comparing
prices will compare a bare 555 IC against a relay module and an LED kit.

**Expected**

- Distinct products must have distinct `product_id`s (bare IC, module, kit).
- If grouping is intentional, `price_comparable` must be `false` when titles/pack
  quantities/roles differ, and the record must expose a grouping reason.

**Root-cause hypothesis**

MPN extraction ("NE555") is used as the grouping key for multi-vendor offer rollup
without a sold-item / role check. `product_identity` correctly returns `unknown`, but
the grouping logic does not consult it.

**Acceptance test**

- For a sample of 100 MPN-derived groups, assert all offers share role/family and
  packaging basis before `price_comparable:true`.
- `get_product(44767)` should split into IC vs module vs kit records, or mark offers
  non-comparable.

---

### B03 — `search_by_mpn` returns accessories and modules as the part

**Severity:** P0 (wrong results for a precision API)
**Tool:** `search_by_mpn`

**Repro**

```js
search_by_mpn({ mpn: "SG90", limit: 8 })
search_by_mpn({ mpn: "AMS1117", limit: 8 })
```

**Observed — `SG90`**

| Title | `product_identity.role` | Price |
|---|---|---|
| Servo Mount Holder Acrylic Bracket for SG90/MG90 | accessory | 25 |
| Servo Mount Holder Bracket for SG90/MG90 – 2pcs | accessory | 39 |
| Wheel for SG90/MG90/FS90R Servo Motors | accessory | 99 |
| SG90 9G Micro Servo Motor 180° | component | 99 |

**Observed — `AMS1117`**

Bare SOT-223 regulators (3.7–9.44 INR) mixed with 3.3V power-supply modules
(19–21.24 INR).

**Expected**

`search_by_mpn` is documented as resolving a part number "without fuzzy text matching"
and should honour the identity policy (`modeled_families` require sold-item identity).
Accessories should be excluded by default or clearly partitioned, and modules should not
be offered as the IC.

**Root-cause hypothesis**

MPN matching is title-token based (any title containing the token matches) and does not
apply the `product_identity.role` filter that `search_components` uses for modeled
families. `allow_ambiguous` only gates family tokens (ESP32/STM32), not role mismatch.

**Acceptance test**

- `search_by_mpn("SG90")` default result set contains zero `role:"accessory"` rows.
- `search_by_mpn("AMS1117")` default result set contains only `role:"component"` rows,
  or returns `mpn_ambiguous` with a partition.

---

### B04 — `resolution` and `thread_size` are completely unfilterable

**Severity:** P0 (two advertised keys are 100% unusable)
**Tools:** `count_components`, `search_components`, `describe_spec`

**Repro**

```js
count_components({ filters: { resolution: "128x64" } })
count_components({ filters: { thread_size: "M3" } })
count_components({ filters: { resolution: 128 } })
count_components({ filters: { thread_size: 3 } })
describe_spec({ key: "resolution" })
describe_spec({ key: "thread_size" })
```

**Observed**

```
resolution="128x64"  → invalid_filter_value
resolution=128       → ok, n=0
resolution="128"     → ok, n=0
thread_size="M3"     → invalid_filter_value
thread_size=3        → ok, n=0
thread_size="3"      → ok, n=0
```

Error message is self-contradictory:

> `Filter 'resolution' expects a numeric value; got '128x64'. This key is string.
> Engineering suffixes are accepted (e.g. 10).`

`describe_spec` confirms the keys are `type:"string"` with real values:

```json
"resolution":  {"values": null, "range": null,
                "sample_raw_strings": ["0.1", "1024 x 768 Pixels", "1024 × 600"]}
"thread_size": {"values": null, "range": null,
                "sample_raw_strings": ["M10", "M170", "M185", "M2", "M2.5"]}
```

**Expected**

String keys support equality / `in` (case-insensitive, normalized whitespace) against
their stored values. At minimum, a string key must never demand a numeric value.

**Root-cause hypothesis**

The filter compiler coerces every non-enum key through the numeric parser. `type` is
read from the spec definition but only used for `enum` vs "numeric"; the `string` branch
is missing.

**Acceptance test**

- `filters:{thread_size:"M3"}` returns matches.
- `filters:{resolution:"1024x600"}` matches `1024 × 600` variants after normalization.
- `filters:{resolution:{in:["128x64","320x240"]}}` works.
- `describe_spec` for both keys returns either `values` or useful `sample_raw_strings`
  in a filterable form.

---

### B05 — Three advertised filter keys do not exist

**Severity:** P0 (metadata points at an invalid API)
**Tools:** `describe_catalog` → `count_components`

**Repro**

```js
describe_catalog({})
// taxonomy: relay.filterable_attributes      → contact_voltage_v
//           power.filterable_attributes      → input_voltage_v
//           crystals.filterable_attributes   → load_capacitance_pf

count_components({ filters: { contact_voltage_v: 12 } })
count_components({ filters: { input_voltage_v: 12 } })
count_components({ filters: { load_capacitance_pf: 10 } })
```

**Observed**

All three → `error_code:"unknown_filter_key"`.

The full taxonomy advertises **52** distinct attribute keys; the global spec taxonomy has
**49**. These are the 3 extras.

**Expected**

Either register these keys in the promoted taxonomy with a real extractor, or remove
them from `taxonomy[].filterable_attributes`.

**Acceptance test**

- For every key in `taxonomy[].filterable_attributes`, a probe call must not return
  `unknown_filter_key`. Add this as a contract test.

---

### B06 — Bare list values are stringified and silently return 0

**Severity:** P0 (silent wrong result, easy for an LLM caller to hit)
**Tools:** `count_components`, `search_components`

**Repro**

```js
count_components({ filters: { diode_type: ["schottky", "zener"] } })        // 0
count_components({ filters: { diode_type: { in: ["schottky","zener"] } } }) // 3358
count_components({ filters: { diode_type: ["schottky"] } })                 // 0
count_components({ filters: { resistance_ohm: [1, 10] } })                 // invalid_filter_value
```

**Observed**

`filters_applied.filters` echoes the list as a **string**:

```json
{"diode_type": "['schottky', 'zener']"}
```

For 2+ elements a `filter_value_coerced` record is added:

```json
{"key":"diode_type","from":["schottky","zener"],"to":"['schottky','zener']"}
```

For a 1-element list the record is **absent** (B30). The response also emits a
misleading `filter_value_not_present` warning suggesting closest populated values.

**Expected**

A list value for an enum key should be treated as `in`, or rejected with
`invalid_filter_value`. It must never become a literal string and return 0.

**Root-cause hypothesis**

The value normalizer calls `str(value)` for enum keys before checking whether the input
was a list. Numeric keys have an `isinstance(list)` check; the enum path does not.

**Acceptance test**

- Bare list on enum key returns the same count as `{in: […]}`.
- No `filter_value_coerced.to` entry may be a Python-repr string (`"['…']"`).

---

## P1 — reproducible wrong results / metadata contradicts behavior

### B07 — Declared spec coverage is unreachable for 44/49 keys

**Severity:** P1 (coverage numbers are not trustworthy)
**Tools:** `describe_spec` / `health` vs filters

**Repro**

```js
describe_spec({ key: "resistance_ohm" })            // coverage_count: 14400
count_components({ filters: { resistance_ohm: {} }, include_vendor_specs: false }) // 5079
count_components({ filters: { resistance_ohm: { gte: -1e18, lte: 1e18 } },
                   include_vendor_specs: false })   // 5079  ← still 5079
```

**Observed (promoted-only, vendor fallback off)**

| Key | Declared `coverage_count` | Reachable by presence filter | Reachable by full range |
|---|---:|---:|---:|
| resistance_ohm | 14,400 | 5,079 | 5,079 |
| tolerance_pct | 21,144 | 11,205 | 11,205 |
| vds_v | 4,725 | 2,390 | 2,390 |
| id_a | 4,946 | 2,613 | 2,613 |
| cells | 1,364 | 789 | n/a (enum) |
| chemistry | 2,404 | 1,377 | n/a (enum) |
| channel_type | 4,764 | 2,431 | n/a (enum) |
| power_w | 8,043 | 7,189 | 7,189 |
| packaging (`package`) | 24,092 | 24,092 | ✅ |

44 of 49 keys have `declared > reachable`. For resistance, only ~35% of products claimed
to carry the spec can be matched by any query.

**Expected**

`coverage_count` should describe the same population the filter engine can match.
Widening a numeric range must not exclude rows that are claimed to have a value.

**Root-cause hypothesis**

One of:
1. `coverage_count` is computed from all `product_specs` rows, while the filter uses a
   "usable evidence" subset (promoted, numeric-castable).
2. Values stored as strings (`"10k"`, `"4R7"`, `"±5%"`) fail numeric cast to `REAL` and
   become `NULL`, so range predicates drop them silently.

Note: `package` matches exactly, which supports the "numeric cast failure" theory.

**Acceptance test**

- For every numeric key: `count({filters:{key:{gte:<min-1>,lte:<max+1>}}}) == coverage_count`.
- Instrument the cast path to count dropped rows and surface them as a warning.

---

### B08 — Three different product counts per category

**Severity:** P1 (category metadata is self-contradictory)
**Tools:** `describe_catalog` vs `count_components`

**Repro**

```js
const c = await describe_catalog({});
// c.data.taxonomy.find(t => t.id === "resistors").products        → 16133
// c.data.products_by_spec_category.resistors                      → 17997

count_components({ category: "resistors" })                    // 4595
count_components({ category: "resistors", include_related: true }) // 16133
```

**Observed**

- 11/32 categories disagree with the default category query:

| Category | taxonomy | products_by_spec_category | default query |
|---|---:|---:|---:|
| resistors | 16,133 | 17,997 | 4,595 |
| switches | 3,909 | 2,297 | 2,733 |
| motor | 3,191 | 2,320 | 2,455 |
| battery | 2,860 | 2,730 | 1,592 |
| sensors | 4,869 | 4,626 | 3,298 |
| connectors | 5,793 | 5,905 | 3,528 |
| capacitors | 7,168 | 8,639 | 6,080 |

- `products_by_spec_category` vs `taxonomy[].products` disagree for **31 of 32**
  categories.
- Sum: taxonomy 106,377 · actual default queries 85,856.

**Expected**

One documented meaning per number. The tool description says
`products_by_spec_category` "lists the values accepted by the `category` parameter", so
the default query should match it (or the field must be renamed/labelled
"with related items").

**Root-cause hypothesis**

`taxonomy[].products` is a vendor-category count; `products_by_spec_category` is a
spec-category count; the default query applies sold-item identity filtering
(`component_when_modeled`). Three different pipelines, no field names distinguishing
them.

**Acceptance test**

- `count({category:X}) == products_by_spec_category[X]` for all categories, or add
  explicit `products_component` / `products_with_related` fields and document them.

---

### B09 — `describe_spec` case/space silently reads the vendor layer

**Severity:** P1 (15× wrong coverage, bogus units)
**Tool:** `describe_spec`

**Repro**

```js
describe_spec({ key: "voltage" })
describe_spec({ key: "Voltage" })
describe_spec({ key: " voltage " })
describe_spec({ key: "Voltage_v" })
```

**Observed**

| Input | `data.key` | `type` | `coverage_count` | `unit` |
|---|---|---|---:|---|
| `voltage` | voltage | enum | 3,135 | `V` |
| `Voltage` | voltage | float | **46,322** | **`v/9v/12v`** |
| `VOLTAGE` | voltage | float | 46,322 | `v/9v/12v` |
| `" voltage "` | voltage | float | 46,322 | `v/9v/12v` |
| `Voltage_v` | voltage_v | float | 11,310 | `v/5v` |

`data.key` says `voltage`, but the payload is vendor-layer data for a different
key/population. Meanwhile `filters:{Voltage:12}` → `unknown_filter_key` (case-sensitive).

**Expected**

- Key lookup is normalized once (case + whitespace + alias), then reads the promoted
  layer unless `vendor:true`.
- The response must not report `key:"voltage"` with `voltage_v`/vendor statistics.
- Filter keys and `describe_spec` keys should use the same normalization rules.

**Root-cause hypothesis**

The exact promoted match fails on case/whitespace; the fallback does a case-insensitive
search across **all** keys including the vendor layer, then returns the first hit while
keeping the requested `key` label.

**Acceptance test**

- `describe_spec({key:"Voltage"})` is byte-identical to `describe_spec({key:"voltage"})`.
- `unit` values must match the registry (`V`), never a slash-joined vendor string.
- Property test: for every key, `describe_spec(key) === describe_spec(key.toUpperCase())`.

---

### B10 — 32/175 advertised per-category enum values are dead

**Severity:** P1
**Tools:** `describe_catalog` vs filters

**Repro**

```js
// For every taxonomy[].filterable_attributes[].values, probe:
count_components({ category: <cat>, filters: { <key>: <value> } })
```

**Observed — 32 zero-result values (18% of 175 probed)**

```
resistors/package=radial              capacitors/package=1812
capacitors/package=radial             capacitors/package=axial
capacitors/package=snap-in            capacitors/package=through-hole
capacitors/capacitor_type=supercapacitor
inductors/package=radial              inductors/package=axial
inductors/package=through-hole        diodes/diode_type=other
integrated-circuits/package=module    sensors/interface=other
sensors/sensor_type=other             connectors/connector_type=other
displays/interface=other              displays/touch=no
leds/package=3mm                      leds/package=5mm
leds/package=through-hole             leds/color=other
modules/interface=other               battery/cells=16s
motor/voltage=14.8                    fuses/fuse_type=blade
fuses/fuse_type=other                 crystals/package=hc-49
crystals/package=5032                 crystals/package=3225
crystals/package=2520                 crystals/package=2016
crystals/package=through-hole
```

Also note `fuse_type.blade`: `describe_spec` claims **125** products, filter returns 0.

**Expected**

Every advertised value either matches ≥1 row or is not advertised. If a value is
supported by the schema but currently empty, mark it explicitly (e.g.
`"status":"no_data"`) rather than listing it as available.

**Acceptance test**

- Contract test: for each `taxonomy[].filterable_attributes[].values[]`, assert
  `count(category, {key:value}) > 0`; allow an explicit `no_data` allowlist.

---

### B11 — `describe_spec.values` counts disagree with filtering (101/110)

**Severity:** P1
**Tools:** `describe_spec` vs `count_components`

**Repro**

```js
describe_spec({ key: "channel_type" })  // n_channel: 3200, p_channel: 1564
count_components({ filters: { channel_type: "n_channel" } }) // 1609
count_components({ filters: { channel_type: "p_channel" } }) // 822

describe_spec({ key: "fuse_type" })    // blade: 125
count_components({ filters: { fuse_type: "blade" } })        // 0
```

**Observed — 101 of 110 declared values mismatch**

| Key.value | declared | filtered | delta |
|---|---:|---:|---:|
| channel_type.n_channel | 3,200 | 1,609 | −1,591 |
| channel_type.p_channel | 1,564 | 822 | −742 |
| capacitor_type.tantalum | 287 | 38 | −249 |
| chemistry.liion | 1,307 | 686 | −621 |
| chemistry.lifepo4 | 315 | 120 | −195 |
| cells.3s | 246 | 152 | −94 |
| connector_type.terminal | 736 | 420 | −316 |
| sensor_type.position | 238 | 180 | −58 |
| fuse_type.blade | 125 | **0** | −125 |
| color.red | 271 | 448 | +177 (vendor fallback) |
| package.to-252 | 93 | 815 | +722 (vendor fallback) |

**Expected**

Either `values` counts the same population the filter matches, or the payload labels the
layer (`promoted` vs `with_vendor_fallback`) and the filter default is documented.

**Root-cause hypothesis**

`values` is computed on the promoted layer; the filter defaults to
`include_vendor_specs:true` (adds rows) **and** enum normalization splits values across
variants (removes rows). Both effects are present at once, so neither direction is
consistent.

**Acceptance test**

- `describe_spec` accepts `include_vendor_specs` and reports both counts.
- Assert `values[v] == count({key:v}, include_vendor_specs:<same>)` for all values.

---

### B12 — `channel_type` stores un-normalized variants

**Severity:** P1
**Tools:** filters + `describe_spec`

**Repro**

```js
count_components({ filters: { channel_type: "n_channel" } })
count_components({ filters: { channel_type: "n channel" } })
count_components({ filters: { channel_type: "n-channel" } })
count_components({ filters: { channel_type: "N Channel" } })
```

**Observed**

```
"n_channel" → 1609     "n channel" → 573     "n-channel" → 1
"p_channel" →  822     "p channel" → 197     "p-channel" → 1
```

`describe_spec` reports only `{n_channel: 3200, p_channel: 1564}` — no filter reaches
3,200. The remaining ~1,000 rows are under other spellings (e.g. `N-Ch`).

**Expected**

Enums are normalized on write (underscore/space/hyphen, case, aliases) or the filter
normalizes all variants. `describe_spec` must not merge counts it cannot address.

**Root-cause hypothesis**

Enum extraction bypasses the normalizer used by the filter, so raw title strings are
stored. The metadata aggregator groups by a normalized key, inflating the declared count.

**Acceptance test**

- `count(eq "n_channel") + count(eq "p_channel") == coverage_count` for the key.
- All variants resolve to the canonical value.

---

### B13 — `eq` silently overrides `gte`/`lte`

**Severity:** P1 (silent constraint loss)
**Tools:** filters

**Repro**

```js
count_components({ filters: { resistance_ohm: { gte: "10k", lte: "1M", eq: "20k" } } }) // 48
count_components({ filters: { resistance_ohm: "20k" } })                                // 48
```

**Observed**

The range is ignored; the result is exactly the `eq` result.

**Expected**

Either AND the constraints (result 0 here, since 20k is inside 10k–1M — so still 48),
but crucially: if the range were contradictory (`gte:1M, eq:20k`) it must return 0, not
48. Currently `{eq:20k, gte:1M}` still returns 48.

**Root-cause hypothesis**

The operator normalizer collapses `{eq, gte, lte}` into a single equality when `eq` is
present.

**Acceptance test**

- `{eq:20000, gte:1e6}` returns 0.
- `{eq:20000, gte:10000, lte:100000}` returns the eq count (AND semantics).
- Or reject mixed `eq` + range with `invalid_filter_value`.

---

### B14 — Empty operator dict `{}` has undocumented, type-dependent semantics

**Severity:** P1
**Tools:** filters

**Repro**

```js
count_components({ filters: { resistance_ohm: {} } })  // 5,975
count_components({ filters: { resistance_ohm: { gte: 0 } } }) // 5,975
count_components({ filters: { resistance_ohm: null } })  // 0
count_components({ filters: {} })                        // 125,508

count_components({ filters: { package: {} } })           // 50,627 (key present, incl. vendor)
count_components({ filters: { diode_type: {} } })        // 4,489
```

**Observed**

- Numeric key + `{}` = `gte 0` (excludes negatives and non-castable values).
- Enum key + `{}` = "key present" (includes vendor fallback).
- `null` = 0 results.

No warning or documentation explains any of this. `{}` on `resistance_ohm` excludes
~8,800 products that are claimed to have the spec (see B07).

**Expected**

`{}` is rejected (`invalid_filter_value: empty operator set`) or documented as
"key present" with a consistent implementation across types.

**Acceptance test**

- `{key:{}}` behavior is identical for numeric/enum/string keys, or errors.

---

### B15 — Unknown `vendor_filters` key returns silent 0

**Severity:** P1 (typos indistinguishable from no-match)
**Tool:** filters

**Repro**

```js
count_components({ vendor_filters: { typo_key: { gte: 20 } } })           // ok, 0
count_components({ vendor_filters: { max_discharge_current_a: { gte: 20 } } }) // ok, 0
count_components({ vendor_filters: { power: 0.1 } })                      // ok, 764 (valid)
```

**Expected**

Unknown vendor keys → `unknown_filter_key` with valid-key suggestions, matching the
behavior of promoted `filters`. Or at least a `vendor_filter_key_not_found` warning.

**Acceptance test**

- Typo key returns `ok:false`, `error_code:"unknown_filter_key"`.
- Valid key with no matching values returns `ok:true`, 0, plus a `no_matches` warning.

---

### B16 — `sort` changes `total_matches`

**Severity:** P1 (breaks pagination/count contracts)
**Tool:** `search_components`

**Repro**

```js
search_components({ price_max: 0 })                     // total_matches: 12
search_components({ price_max: 0, sort: "price_asc" })  // total_matches: 0
count_components({ price_max: 0 })                      // total_matches: 12
```

Also `search_components({sort:"price_asc"})` → 125,496 vs catalog 125,508 (12 zero-price
rows excluded).

**Expected**

`total_matches` describes the filter result set and is sort-independent. If price-sorted
pagination excludes zero-price rows, either:
- keep `total_matches` at 12 and return 0 rows with a warning, or
- add `total_sorted` alongside `total_matches`, documented.

**Root-cause hypothesis**

The zero-price exclusion is applied in the ORDER BY/WHERE branch only when
`sort=price_asc|price_desc`, and the count query reuses the same SQL.

**Acceptance test**

- `total_matches` identical for all three sort values on the same filters.

---

### B17 — Capacitor `capacitance` alias is off by 10⁶

**Severity:** P1 (contradictory values in the same row)
**Tool:** `search_components` rows

**Repro**

```js
const r = await search_components({ category: "capacitors", limit: 50, refresh: false });
// compare r.specs.capacitance_uf with r.specs.capacitance
```

**Observed — 44 of 47 pairs wrong**

| product_id | `capacitance_uf` | `capacitance` | expected F | ratio |
|---|---:|---:|---:|---:|
| 63680 | 0.082 | 8.2e-14 | 8.2e-8 | 1e-6 |
| 98740 | 1 | 1e-11 | 1e-6 | 1e-5 |
| 85634 | 2.2 | 2.2e-12 | 2.2e-6 | 1e-6 |
| 63140 | 0.015 | 1.5e-14 | 1.5e-8 | 1e-6 |

Median ratio: `1e-6` (correct rows: `1.0`).

**Expected**

`capacitance` (vendor alias) should equal `capacitance_uf * 1e-6`. If units are
ambiguous, the alias must carry an explicit unit (e.g. `capacitance_f`).

**Root-cause hypothesis**

The vendor value is converted µF→F and then the unit conversion is applied a second
time.

**Acceptance test**

- For all capacitor rows: `abs(capacitance / (capacitance_uf*1e-6) - 1) < 0.01`.
- Property test across a 1,000-row sample.

---

### B18 — `specs` shape differs between search and detail

**Severity:** P1 (same product, two shapes)
**Tools:** `search_components`, `get_product`

**Repro**

```js
const s = await search_components({ filters: { resistance_ohm: 10000 }, limit: 50 });
// row 46841 .specs →
// {power_w:"0.1", resistance_ohm:"10000", power:"0.1", resistance:"10000"}

await get_product({ product_id: 46841 });
// .specs → {power_w:"0.1", resistance_ohm:"10000"}   (alias keys omitted)
```

**Observed**

- Search rows merge promoted + vendor alias keys into `specs` (4 keys).
- `get_product.specs` returns only promoted keys (2 keys), while
  `get_product.spec_details` still contains the alias entries.
- `spec_sources` is present only in search rows.

**Expected**

One documented shape. Recommended: `specs` = canonical/promoted only in both; add
`vendor_specs` / `related_specs` for aliases (already present in `get_product`).

**Acceptance test**

- For 100 random products, `set(search_row.specs.keys) == set(get_product.specs.keys)`.
- Alias values live only under an explicitly named field.

---

### B19 — Boolean `tolerance` coerced to ±100%

**Severity:** P1 (type validation hole)
**Tool:** filters

**Repro**

```js
count_components({ filters: { resistance_ohm: { eq: 100, tolerance: true } } }) // ok, 3281
count_components({ filters: { resistance_ohm: { eq: 100, tolerance: "0.05" } } }) // ok, 162
count_components({ filters: { resistance_ohm: { eq: 100, tolerance: 0 } } })     // invalid (correct)
```

**Observed**

- `true` is accepted as `1.0` → ±100%, returns 3,281 rows.
- `"0.05"` (string) is accepted.
- `0`, `1.5`, `-0.1` are correctly rejected.

**Expected**

Booleans rejected; numeric strings either accepted consistently everywhere or rejected
everywhere. Document the coercion policy.

**Acceptance test**

- `tolerance:true`, `tolerance:false`, `tolerance:"0.05"`, `tolerance:[0.1]`,
  `tolerance:{}` all return the same error class (or all coerce per a documented rule).

---

### B20 — `resistance_ohm` declared max is unreachable

**Severity:** P1
**Tools:** `describe_spec` vs filters

**Repro**

```js
describe_spec({ key: "resistance_ohm" })                 // range: {min:0.001, max:910000000}
count_components({ filters: { resistance_ohm: { gte: 910000000 } } }) // 0
```

Other declared maxima do resolve: `capacitance_uf` 100,000 → 1 · `rpm` 50,000 → 5 ·
`tolerance_pct` 80 → 1 · `pins` 100 → 19 · `capacity_mah` 100,000 → 15.

**Expected**

Range max derived from actual data, or flagged as the theoretical limit.

**Acceptance test**

- For every numeric key: `count({key:{gte:range.max}}) > 0` or `range.max` is removed /
  labelled.

---

### B21 — `describe_spec(vendor=true)` returns no values/range

**Severity:** P1 (the vendor-inspection path cannot be used)
**Tool:** `describe_spec`

**Repro**

```js
describe_spec({ key: "resistance_ohm", vendor: true })
describe_spec({ key: "max_discharge_current_a", vendor: true })
```

**Observed**

```json
{"key":"resistance_ohm","status":"vendor_reported","type":"float",
 "coverage_pct":12.45,"values":null,"range":null}
{"key":"max_discharge_current_a","status":"vendor_reported",
 "coverage_pct":0.08,"values":null,"range":null}
```

**Expected**

Vendor mode is undocumented as "counts only" — the description says it includes keys not
in the promoted taxonomy, implying inspectability. Return `values` (top N with counts) and
`range` for vendor keys so callers can build `vendor_filters`.

**Acceptance test**

- `describe_spec({key:"max_discharge_current_a", vendor:true}).data.values` is non-empty
  and can be fed back into `vendor_filters` successfully.

---

## P2 — API / contract inconsistencies

### B22 — Schema-validation errors bypass the structured envelope

**Severity:** P2 (breaks agent error handling)
**Tools:** all

**Repro (each throws instead of returning `ok:false`)**

```js
count_components({ category: 123 })
count_components({ filters: "x" })
count_components({ vendor_filters: "x" })
search_components({ query: "a", limit: 5.5 })
search_components({ query: "a", limit: 9223372036854775807 })
search_components({ query: "a", price_max: true })
search_components({ query: "a", price_max: "NaN" })
search_components({ query: "a", in_stock_only: "yes" })
get_product({ product_id: 1.5 })
get_product({ product_id: null })
get_product({ refresh: false })            // missing product_id
search_by_mpn({ mpn: null })
```

**Observed**

```
Error: Invalid arguments for tool "aggregate_search_components":
- limit: Expected an integer
...
```

Raw pydantic/MCP validation text, no `error_code`. Meanwhile semantic errors
(`unknown_filter_key`, `invalid_filter_value`) do use the envelope.

**Expected**

Map argument-validation failures to `ok:false`, `error_code:"invalid_arguments"`, with
per-field details under `details`/`suggestions`.

**Acceptance test**

- A shared test that calls each tool with malformed args and asserts a JSON envelope
  with `ok:false` and a stable `error_code`.

---

### B23 — Unknown top-level arguments silently ignored

**Severity:** P2 (typos produce unfiltered results)
**Tools:** `search_components`, `count_components`

**Repro**

```js
search_components({ query: "resistor", bogus_option: 1 })          // ok, 16516
count_components({ filters: { resistance_ohm: 10000 }, bogus: true }) // ok, 126
count_components({ filters: {}, include_related: true })            // accepted (not in signature)
```

**Expected**

Unknown kwargs rejected (`invalid_arguments`) or echoed in `warnings`. At minimum, the
`count_components` signature should list `include_related` since it is honoured.

**Acceptance test**

- Typo'd parameter name produces a warning or error.
- Schema (tool signature) and runtime-accepted params match exactly.

---

### B24 — Alias expansion disabled when `category` is present

**Severity:** P2 (same filter, different semantics)
**Tools:** filters

**Repro**

```js
count_components({ filters: { voltage: 50 } })
// n=2551, filters_applied.filter_expanded = {voltage:[voltage,voltage_v], per_key_counts:{...}}

count_components({ category: "capacitors", filters: { voltage: 50 } })
// n=1666, filters_applied.filter_expanded = undefined
```

**Observed**

Presence of `category` suppresses `filter_expanded` entirely. The same filter expression
is broader without a category (2,551 vs 1,666 for `voltage:50`).

**Expected**

Either always expand (and intersect with applicable keys) or never expand; document it.
`filter_expanded` must always be present when expansion is supported.

**Acceptance test**

- `count({filters:{voltage:50}})` and `count({category:X, filters:{voltage:50}})` either
  agree on expansion semantics or `filter_expanded` consistently reports the decision.

---

### B25 — `filter_expanded.per_key_counts` does not reconcile

**Severity:** P2 (misleading diagnostics)
**Tools:** filters

**Repro**

```js
count_components({ filters: { voltage: 12 } })
```

**Observed**

```json
"filter_expanded": {
  "voltage": ["voltage", "voltage_v"],
  "per_key_counts": { "voltage": 533, "voltage_v": 213 }
}
```
`total_matches` = **2,524**. Sum of `per_key_counts` = 746. A union cannot exceed the sum
of its parts.

The same pattern appears for `voltage:5` (2,179 vs 82+586) and `voltage:24` (808 vs
227+100).

**Expected**

`per_key_counts` should sum to at least `total_matches`, or be renamed to describe what
it actually counts (e.g. "distinct matched values").

**Acceptance test**

- `sum(per_key_counts) >= total_matches` for all expanded filters.

---

### B26 — Vendor-spec key count contradicts `health`

**Severity:** P2
**Tools:** `describe_catalog`, `health`

**Repro**

```js
health({})            // data.vendor_spec_counts.keys = 43879
describe_catalog({})  // data.vendor_spec_key_count = 6055
describe_catalog({summary:true}) // same: 6055
```

**Expected**

One definition, one number. If `health` counts raw distinct strings and
`describe_catalog` counts normalized keys, label them (`raw_keys` vs `normalized_keys`).

**Acceptance test**

- Both tools agree, or field names make the distinction explicit.

---

### B27 — Partial live refresh reported without a warning

**Severity:** P2 (stale prices presented as refreshed)
**Tools:** `search_components`, `get_product`

**Repro**

```js
search_components({ query: "resistor", limit: 50, refresh: true })
```

**Observed**

```json
"price_stock_refresh": {
  "status": "partial",
  "refreshed_products": 3,
  "cached_products": 47,
  "verified_pct": 6,
  "vendors_ok": ["makerbazar","quartzcomponents","rees52","robokits","robu"],
  "vendors_failed": [],
  "message": "Refreshed price/stock for 3 of 50 listing(s) (6.0% verified)."
}
```
`warnings: []`. `price_source` per row is a mix of `live` and `cache`.

**Expected**

Add `warnings:[{code:"price_refresh_partial", verified_pct:6, …}]` so callers that only
read `warnings` know prices are mostly cached.

**Acceptance test**

- Any `status != "live"` produces a warning on the top-level response.

---

### B28 — Documented MPN example returns nothing

**Severity:** P2 (docs)
**Tool:** `search_by_mpn`

**Repro**

```js
search_by_mpn({ mpn: "RMCS-2077" })  // documented example
```

**Observed**

`ok:true`, `total_matches:0`, `suggested_mpns: []`, warning `mpn_not_found`.

**Expected**

Either the example is a real catalog MPN, or the docs use one that resolves. Also,
`mpn_not_found` should return did-you-mean suggestions when any near match exists
(it does for `MPU605`, `WR06X1002FT`).

**Acceptance test**

- Every MPN used in tool documentation resolves to ≥1 row.

---

### B29 — `get_product` omits refresh status when `refresh:false`

**Severity:** P2
**Tool:** `get_product`

**Repro**

```js
get_product({ product_id: 46841, refresh: false }) // data.price_stock_refresh = null
get_product({ product_id: 46841 })                 // status:"live"
```

**Observed**

`price_stock_refresh` is `null` when refresh is skipped, so a caller cannot tell whether
the price is fresh or cached. Search rows still expose `price_source`.

**Expected**

Always return a refresh block (`{status:"skipped"|"cache"|"live"|"partial", …}`).

**Acceptance test**

- `price_stock_refresh` is a non-null object in every successful `get_product` response.

---

### B30 — `filter_value_coerced` is incomplete

**Severity:** P2 (audit trail unreliable)
**Tools:** filters

**Repro**

```js
count_components({ filters: { diode_type: ["schottky"] } })           // no coercion record
count_components({ filters: { diode_type: ["schottky","zener"] } })   // coercion recorded
count_components({ filters: { diode_type: "Schottky" } })             // no record (case-normalized)
count_components({ filters: { cells: "1" } })                         // record: "1" → "1s" (good)
```

**Expected**

Every value transformation (stringification, case folding, unit conversion, alias
expansion) appears in `filters_applied.filter_value_coerced` / `filter_expanded`, or none
do.

**Acceptance test**

- For every key, feed a non-canonical value and assert a coercion record exists whenever
  the applied value differs from the input.

---

## Verified working — do not "fix"

These behaviours were tested and found correct, so they should not be changed while
addressing the items above:

- `count_components` vs `search_components` totals: 30/30 random combinations matched
  exactly (filters, query, category, price, stock).
- Pagination: 12 pages × 50 = 600 rows, 600 unique, no gaps; small result sets terminate
  with `next_cursor:null`; `truncated` matches `next_cursor`.
- Cursor binding: cross-query and cross-sort cursors → `stale_cursor`; repeated use of the
  same cursor is idempotent; changing `limit` with the same cursor is allowed.
- Determinism: 6/6 sequential and 6 parallel `refresh:true` calls returned identical
  totals, IDs and prices.
- Row fidelity: 0 rows failed their own filter across sampled queries
  (`color`, `package`, `channel_type`, numeric ranges).
- Limit clamping: 51 / 100 / 2³¹ → 50 rows + `limit_clamped` warning.
- Payload size claims: `compact` 50 rows = 20,323 bytes (<25 KB);
  `describe_catalog(summary=true)` = 5,112 bytes (<6 KB).
- `in_stock_only`, `price_max`, and `count`/`search` parity hold (except B16).
- SQL-injection-shaped queries are safely tokenized; unicode, emoji, newlines and
  tabs are handled; 5,000-char queries/values do not crash.
- MPN normalization: `MPU6050`, `mpu6050`, `mpu-6050`, `mpu 6050` all resolve to the
  same 17 listings; `mpn_did_you_mean` suggestions work.
- `mpn_family_token` correctly rejects `ESP32`/`STM32` unless `allow_ambiguous:true`.
- `search_by_mpn` pagination and limit clamping behave correctly.
- `price_asc`/`price_desc` ordering is correct and deterministic; no null prices appear.
- `get_product` `vendor_offers` prices match the product top-level price.
- Multi-term query relaxation is intentional: dropping terms emits
  `query_relaxed` + `query_low_confidence` warnings and `query_tokens_dropped`.
- Default vendor fallback (`include_vendor_specs:true`) is intentional — the issue is
  only that `describe_spec` does not label its layer (B07/B11).
- Category identity filtering (`include_related:false`) is intentional — the issue is
  only that metadata does not disclose it (B08).

---

## Suggested fix order

1. **B01** (crash) — sanitize/strip control characters before FTS.
2. **B02–B03** (identity) — gate offer rollup and MPN results on `product_identity.role`;
   split mixed groups.
3. **B04–B06** (unusable keys, invalid metadata, silent list coercion) — these are cheap,
   high-visibility fixes for agent callers.
4. **B07–B12** (coverage/metadata truthfulness) — pick one source of truth per number and
   label the layer everywhere.
5. **B13–B21** (filter semantics) — tighten normalization and operator validation.
6. **B22–B30** (contract polish).

## Test harness

A minimal re-runner for the contract checks (install into the MCP client test suite):

```js
// 1. Advertised keys must be filterable
const cat = await describe_catalog({});
for (const t of cat.data.taxonomy) {
  for (const a of t.filterable_attributes || []) {
    const r = await count_components({ category: t.id, filters: { [a.key]: 1 } });
    assert(r.error_code !== "unknown_filter_key", `dead key ${t.id}/${a.key}`);
    for (const v of a.values || []) {
      const rv = await count_components({ category: t.id, filters: { [a.key]: v } });
      assert(rv.total_matches > 0, `dead enum ${t.id}/${a.key}=${v}`);
    }
  }
}

// 2. Declared coverage must be reachable
for (const k of cat.data.spec_keys.map(s => s.key)) {
  const d = await describe_spec({ key: k });
  if (!d.data.range) continue;
  const r = await count_components({
    filters: { [k]: { gte: d.data.range.min, lte: d.data.range.max } },
    include_vendor_specs: false,
  });
  assert(r.total_matches === d.data.coverage_count, `coverage gap ${k}`);
}

// 3. Enum values must round-trip
for (const k of cat.data.spec_keys.filter(s => s.type === "enum").map(s => s.key)) {
  const d = await describe_spec({ key: k });
  for (const [v, n] of Object.entries(d.data.values || {})) {
    const r = await count_components({ filters: { [k]: v } });
    assert(r.total_matches === n, `value count ${k}.${v}: ${r.total_matches} != ${n}`);
  }
}

// 4. count/search parity + cursor independence
const combos = [/* filters, query, category, price_max, in_stock_only */];
for (const c of combos) {
  const a = await count_components(c);
  const b = await search_components({ ...c, limit: 5 });
  assert(a.total_matches === b.total_matches, "count/search mismatch");
  const c2 = await search_components({ ...c, limit: 5, sort: "price_asc" });
  assert(a.total_matches === c2.total_matches, "sort changed total_matches");
}

// 5. Malformed args must return an envelope
const bad = [
  () => count_components({ category: 123 }),
  () => search_components({ query: "a", limit: 5.5 }),
  () => get_product({ product_id: null }),
];
for (const f of bad) {
  let threw = false;
  try { const r = await f(); if (r.ok === undefined) threw = true; }
  catch { threw = true; }
  assert(!threw, "argument validation escaped the envelope");
}
```
