# PILOT REPORT ROUND 4: Agent-First Spec-Driven MCP Query Architecture

## Executive Summary
Round 4 refactored the FastMCP component query server into an **agent-first API**, resolving client wire-level parsing issues, enforcing uniform response/error envelopes, adding explicit filter operators (`in`, `gte`, `lte`, `eq`, `tolerance`), implementing opaque cursor-based pagination, and introducing four specialized discovery, sizing, and introspection tools.

The resulting surface exposes exactly 6 tools: `describe_catalog`, `describe_spec`, `count_components`, `get_product`, `search_components`, and `list_spec_keys` (backward compatibility).

---

## 1. Wire-Fix Confirmation
- **Single Content Block per Call**: FastMCP return type is declared with Pydantic `ToolResponseEnvelope`. Every tool call returns **exactly 1 TextContent block** containing the serialized envelope JSON document, rather than splitting array elements across multiple blocks.
- **OutputSchema Validation**: Every tool auto-generates a self-describing JSON `outputSchema` (16 properties). Structured content validates identically against the schema; clients reading either `structuredContent` or `content[0].text` parse the same document.

---

## 2. Response Envelopes (Raw JSON)

### 2.1 Successful Search Envelope
```json
{
  "ok": true,
  "data": {
    "results": [
      {
        "product_id": 44859,
        "title": "12V 200RPM Gear Motor Centre Shaft DC Geared Motor Heavy Duty Motor for Robotics - ER024",
        "vendor": "REES52",
        "price": 119.0,
        "available": true,
        "stock_status": "in_stock",
        "product_url": "https://rees52.com/products/12v-200rpm-gear-motor-centre-shaft-dc-geared-motor-heavy-duty-motor-for-robotics-er024",
        "specs": {
          "capacity_mah": null,
          "cells": null,
          "channels": null,
          "chemistry": null,
          "current_a": null,
          "rpm": "200",
          "voltage": "12"
        },
        "best_price": 119.0,
        "best_vendor": "REES52",
        "top_offers": [
          {
            "vendor": "REES52",
            "price": 119.0,
            "available": true,
            "stock_status": "in_stock",
            "url": "https://rees52.com/products/12v-200rpm-gear-motor-centre-shaft-dc-geared-motor-heavy-duty-motor-for-robotics-er024"
          }
        ]
      }
    ]
  },
  "total_matches": 24,
  "returned": 1,
  "truncated": true,
  "next_cursor": "MTo0OWM5NjhhYg==",
  "filters_applied": {
    "voltage": "12",
    "rpm": {
      "gte": 190,
      "lte": 210
    }
  },
  "coverage": {
    "voltage": {
      "present": 800,
      "of": 106322,
      "pct": 0.75
    },
    "rpm": {
      "present": 656,
      "of": 106322,
      "pct": 0.62
    }
  },
  "warnings": [
    {
      "code": "filter_key_not_extracted",
      "message": "Filter key 'voltage' has low catalog coverage (0.75%, 800 of 106322 products)."
    },
    {
      "code": "filter_key_not_extracted",
      "message": "Filter key 'rpm' has low catalog coverage (0.62%, 656 of 106322 products)."
    }
  ],
  "server_version": "1.4.0",
  "schema_version": "1.4.0"
}
```

### 2.2 Error Envelope (No Bare Exceptions)
```json
{
  "ok": false,
  "data": null,
  "total_matches": null,
  "returned": null,
  "truncated": null,
  "next_cursor": null,
  "filters_applied": null,
  "coverage": null,
  "warnings": null,
  "server_version": "1.4.0",
  "schema_version": "1.4.0",
  "error_code": "unknown_filter_key",
  "message": "Unknown filter key 'unknown_spec_key'.",
  "valid_keys": [
    "capacity_mah",
    "cells",
    "channels",
    "chemistry",
    "current_a",
    "rpm",
    "voltage"
  ],
  "suggestions": [
    {"key": "capacity_mah"},
    {"key": "cells"},
    {"key": "channels"},
    {"key": "chemistry"},
    {"key": "current_a"},
    {"key": "rpm"},
    {"key": "voltage"}
  ],
  "details": {
    "provided_key": "unknown_spec_key"
  }
}
```

---

## 3. unknown ≠ absent ≠ null Discrimination

| State | Category / Filter | Behavior & Example |
| :--- | :--- | :--- |
| **Matched (`specs[key] = value`)** | `filters={"voltage": "12"}` | Extracted spec present. Every product result exposes all catalog keys; unextracted keys are explicitly `null` (not omitted). |
| **Low Coverage (`filter_key_not_extracted`)** | `filters={"current_a": 1.5}` | Attribute exists in catalog taxonomy but has low coverage (0.40%). Returns matching results along with diagnostic warning: `{"code": "filter_key_not_extracted", "message": "Filter key 'current_a' has low catalog coverage (0.40%, 420 of 106322 products)."}` and precision advisory. |
| **Value Unmatched (`filter_value_not_present`)** | `filters={"chemistry": "unobtainium"}` | Key has high coverage (2.09%), but value matches 0 rows. Returns `ok: true`, `total_matches: 0`, `data.results: []`, warning `filter_value_not_present`, and enum suggestions: `[{"key": "chemistry", "value": "liion", "count": 1196}, {"key": "chemistry", "value": "lipo", "count": 720}, ...]`. |

---

## 4. Input Normalization & Operator Syntax

3 variations of input each normalizing to the identical canonical value:

1. **Operating Voltage (`voltage` &rarr; canonical `"24"`)**:
   - `"24V"` &rarr; `"24"`
   - `"24 v"` &rarr; `"24"`
   - `"24VDC"` &rarr; `"24"`

2. **Capacity (`capacity_mah` &rarr; canonical `5200`)**:
   - `"5200 mAh"` &rarr; `5200`
   - `"5.2Ah"` &rarr; `5200`
   - `"5200mah"` &rarr; `5200`

3. **Cell Count (`cells` &rarr; canonical `"4s"`)**:
   - `"4S"` &rarr; `"4s"`
   - `"4 s"` &rarr; `"4s"`
   - `"4s lipo"` &rarr; `"4s"`

---

## 5. New Tools Output Samples

### 5.1 `describe_catalog()`
```json
{
  "total_products": 106322,
  "products_by_vendor": {
    "robu": 85654,
    "rees52": 8123,
    "quartzcomponents": 6008,
    "makerbazar": 4358,
    "robokits": 2179
  },
  "products_by_spec_category": {
    "unclassified": 97668,
    "motor": 3632,
    "battery": 3512,
    "relay": 1510
  },
  "spec_keys": [
    {
      "key": "chemistry",
      "display_name": "Chemistry",
      "type": "enum",
      "unit": null,
      "coverage_pct": 2.09,
      "count": 2223,
      "values": ["lipo", "liion", "lifepo4", "nimh", "leadacid"]
    },
    {
      "key": "cells",
      "display_name": "Cell Count",
      "type": "enum",
      "unit": null,
      "coverage_pct": 1.18,
      "count": 1255,
      "values": ["1s", "2s", "3s", "4s", "5s", "6s", "7s", "8s", "10s", "12s", "14s", "16s"]
    },
    {
      "key": "capacity_mah",
      "display_name": "Capacity",
      "type": "integer",
      "unit": "mAh",
      "coverage_pct": 1.11,
      "count": 1182,
      "range": {"min": 100.0, "max": 100000.0}
    },
    {
      "key": "voltage",
      "display_name": "Operating Voltage",
      "type": "enum",
      "unit": "V",
      "coverage_pct": 0.75,
      "count": 800,
      "values": ["3.3", "3.7", "5", "6", "7.4", "9", "12", "14.8", "24", "36", "48"]
    },
    {
      "key": "rpm",
      "display_name": "Speed (RPM)",
      "type": "integer",
      "unit": "RPM",
      "coverage_pct": 0.62,
      "count": 656,
      "range": {"min": 4.0, "max": 50000.0}
    },
    {
      "key": "current_a",
      "display_name": "Rated Current",
      "type": "float",
      "unit": "A",
      "coverage_pct": 0.4,
      "count": 420,
      "range": {"min": 0.15, "max": 1684.0},
      "confidence": "low",
      "precision_note": "Advisory only (~70% precision in catalog titles; often reflects driver/module current rather than motor rated current)"
    },
    {
      "key": "channels",
      "display_name": "Channels",
      "type": "integer",
      "unit": null,
      "coverage_pct": 0.3,
      "count": 315,
      "range": {"min": 1.0, "max": 32.0}
    }
  ],
  "currency": "INR",
  "freshness": {
    "oldest_crawl": "2026-10-18T14:55:31.283079+00:00",
    "newest_crawl": "2026-10-19T16:55:52.116058+00:00"
  }
}
```

### 5.2 `describe_spec(key="rpm")`
```json
{
  "key": "rpm",
  "status": "key_extracted_low_coverage",
  "display_name": "Speed (RPM)",
  "type": "integer",
  "unit": "RPM",
  "coverage_pct": 0.62,
  "coverage_count": 656,
  "values": null,
  "range": {
    "min": 4.0,
    "max": 50000.0
  },
  "sample_raw_strings": ["10", "100", "1000", "10000", "106"],
  "warnings": [
    {
      "code": "low_coverage_warning",
      "message": "Attribute has low catalog coverage (0.62% of total products)."
    }
  ]
}
```

### 5.3 `get_product(product_id=44859)`
```json
{
  "product_id": 44859,
  "title": "12V 200RPM Gear Motor Centre Shaft DC Geared Motor Heavy Duty Motor for Robotics - ER024",
  "price": 119.0,
  "available": true,
  "product_url": "https://rees52.com/products/12v-200rpm-gear-motor-centre-shaft-dc-geared-motor-heavy-duty-motor-for-robotics-er024",
  "crawl_timestamps": {
    "when_scraped": "2026-10-18T16:16:17.685133+00:00",
    "when_specs_extracted": "2026-10-19"
  },
  "extracted_specs": {
    "rpm": {
      "value": "200",
      "value_num": 200.0,
      "type": "integer",
      "confidence": 1.0,
      "evidence": "200RPM"
    },
    "voltage": {
      "value": "12",
      "value_num": null,
      "type": "enum",
      "confidence": 1.0,
      "evidence": "12V"
    }
  },
  "spec_extraction_method": {
    "rpm": "regex",
    "voltage": "regex"
  },
  "vendor_offers": [
    {
      "product_id": 44859,
      "vendor": "REES52",
      "title": "12V 200RPM Gear Motor Centre Shaft DC Geared Motor Heavy Duty Motor for Robotics - ER024",
      "price": 119.0,
      "available": true,
      "url": "https://rees52.com/products/12v-200rpm-gear-motor-centre-shaft-dc-geared-motor-heavy-duty-motor-for-robotics-er024",
      "vendor_sku": "ER024",
      "mpn": "ER024"
    }
  ]
}
```

---

## 6. End-to-End Call Chain: `"12v 200rpm motor"`

An agent executes the recommended workflow:
1. **`describe_catalog()`**: Discovers 106,322 products and notes that `voltage` (unit `V`, enum) and `rpm` (unit `RPM`, integer) are active queryable keys.
2. **`describe_spec(key="rpm")`**: Confirms valid integer range [4.0, 50000.0] RPM and 656 matching components.
3. **`count_components(filters={"voltage": "12", "rpm": {"eq": 200, "tolerance": 0.05}}, query="motor")`**:
   - Returns `total_matches: 24`, `filters_applied: {"voltage": "12", "rpm": {"eq": 200, "tolerance": 0.05}}`.
4. **`search_components(filters={"voltage": "12", "rpm": {"eq": 200, "tolerance": 0.05}}, query="motor", limit=2)`**:
   - Returns `total_matches: 24`, `returned: 2`, `truncated: true`, `next_cursor: "Mjo..."`.
   - Result #1: PID `44859` — *"12V 200RPM Gear Motor Centre Shaft DC Geared Motor Heavy Duty Motor for Robotics - ER024"* (₹119.0).
5. **`get_product(product_id=44859)`**:
   - Retrieves full record, regex extraction evidence (`"200RPM"`, `"12V"`), and vendor offers with live URLs.

---

## 7. Acceptance Test Results

Targeted tests in `tests/test_round4_agent_first.py`:
- `test_1_wire_shape_single_content_block`: **PASS** (Single content block, valid JSON array of 7 specs)
- `test_2_count_matches_search_integration`: **PASS** (`count_components(f) == len(search_components(f))` across all 5 sample filters)
- `test_3_error_envelope_unknown_filter_key`: **PASS** (`ok: false`, `error_code: 'unknown_filter_key'`, `valid_keys` populated, non-empty suggestions)
- `test_4_unknown_not_absent_low_coverage_warning`: **PASS** (Preserves matches on low-coverage key, returns coverage metrics and advisory warning)

**Overall Test Suite**: **60 passed in 5.61s** (100% pass across full repository test suite).

---

## 8. Server Line Count Audit

| File | Before Round 4 | After Round 4 | Change |
| :--- | :--- | :--- | :--- |
| `mcp_server/server.py` | 281 lines | 933 lines | +652 lines |

**Additions Breakdown**:
- `ToolResponseEnvelope` Pydantic model with strict field typings for FastMCP wire serialization.
- Comprehensive 5-part API docstrings (Purpose, Call Order, Parameter Syntax, Example Invocation, Behavior When No Matches) on all 6 tools.
- Input normalizer supporting unit conversions (`mAh`/`Ah`, `V`/`VDC`, `S`/`s lipo`), aliases, and case canonicalization.
- Operator parser for `in`, `gte`, `lte`, `gt`, `lt`, `eq`, and `tolerance`.
- 4 new agent-first tools: `describe_catalog`, `describe_spec`, `count_components`, `get_product`.

---

## 9. Deferred to Round 5

The following items are deferred to Round 5 with reasoning:
1. **`browse` & `facets` tools**: Deferred to prevent tool sprawl; `describe_catalog` and `describe_spec` provide sufficient parametric discovery for autonomous agents without interactive facet state machines.
2. **`search_by_mpn` & `search_by_sku` tools**: Redundant with generic query / parametric filters and single product retrieval; can be handled by `search_components(query="<mpn>")` and `get_product`.
3. **`batch_lookup` & `get_related`**: Deferred to Round 5 optimization; single-item `get_product` provides full offer federation.
4. **`coverage_report` & `top_unmet_queries` as MCP tools**: Operational monitoring analytics rather than agent retrieval tools; better suited as scheduled offline maintenance scripts.
5. **Raw attribute persistence in DB**: Requires migrations on `product_specs` and scraper raw stores; deferred to keep Round 4 non-breaking.
