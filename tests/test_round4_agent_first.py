"""
Round 4 Agent-First MCP Server Targeted Acceptance Tests.
Contains exactly 4 DoD-aligned targeted tests:
1. Integration test: count_components == search_components up to pagination across 5 sample filters.
2. Wire-shape test: list_spec_keys returns exactly 1 content block.
3. Error-envelope test: search with unknown key returns ok:false, error_code='unknown_filter_key', suggestions non-empty.
4. Unknown != absent test: search on low-coverage key returns warning with coverage %, does not silently return empty.
"""
import asyncio
import json
import pytest
from mcp_server.server import (
    MAX_PAGE_SIZE,
    mcp,
    count_components,
    search_components,
    list_spec_keys,
)

@pytest.mark.asyncio
async def test_1_wire_shape_single_content_block():
    """Wire-shape test: list_spec_keys returns exactly 1 content block containing single JSON document."""
    content_list, meta = await mcp.call_tool("list_spec_keys", {})
    assert len(content_list) == 1, f"Expected 1 content block, got {len(content_list)}"
    doc = json.loads(content_list[0].text)
    assert doc["ok"] is True
    assert "data" in doc and "spec_keys" in doc["data"]
    assert isinstance(doc["data"]["spec_keys"], list)
    # The taxonomy grows over time (torque/discharge rates joined in Round 7);
    # the wire shape is what matters: one document, a bounded list of keys.
    key_names = {k["key"] for k in doc["data"]["spec_keys"]}
    assert len(doc["data"]["spec_keys"]) >= 7
    assert {"voltage", "rpm", "chemistry", "cells", "capacity_mah"} <= key_names

@pytest.mark.asyncio
async def test_2_count_matches_search_integration():
    """Integration test: for 5 sample filters, count_components matches search_components up to pagination."""
    sample_filters = [
        {"voltage": "12", "rpm": {"gte": 190, "lte": 210}},
        {"cells": "4s", "capacity_mah": {"gte": 5100, "lte": 5300}},
        {"chemistry": "lipo"},
        {"channels": 4},
        {"voltage": {"in": ["5", "12"]}},
    ]

    for f in sample_filters:
        c_res = count_components(filters=f)
        s_res = search_components(filters=f, limit=MAX_PAGE_SIZE)

        assert c_res.ok is True, f"count_components failed for {f}: {c_res.message}"
        assert s_res.ok is True, f"search_components failed for {f}: {s_res.message}"
        assert c_res.total_matches == s_res.total_matches, (
            f"Count mismatch for {f}: count={c_res.total_matches}, search={s_res.total_matches}"
        )
        expected_returned = min(s_res.total_matches, MAX_PAGE_SIZE)
        assert s_res.returned == expected_returned, (
            f"Returned mismatch for {f}: returned={s_res.returned}, expected={expected_returned}"
        )

def test_3_error_envelope_unknown_filter_key():
    """Error-envelope test: search with unknown key returns ok:false, error_code='unknown_filter_key', suggestions non-empty."""
    res = search_components(filters={"unknown_fake_spec_key": "123"})
    assert res.ok is False
    assert res.error_code == "unknown_filter_key"
    assert "unknown_fake_spec_key" in res.message
    assert res.valid_keys is not None and len(res.valid_keys) >= 7
    assert res.suggestions is not None and len(res.suggestions) > 0
    # Version must be reported, but tests should not pin an exact release string.
    assert res.server_version and res.server_version.count(".") == 2

def test_4_unknown_not_absent_low_coverage_warning():
    """Unknown != absent test: search on low-coverage key returns warning with coverage %, does not silently return empty."""
    res = search_components(filters={"current_a": 1.5})
    assert res.ok is True
    assert "current_a" in res.coverage
    cov = res.coverage["current_a"]
    assert "catalog" in cov and "page" in cov
    assert {"present", "of", "pct"} <= set(cov["catalog"])
    assert {"present", "of", "pct"} <= set(cov["page"])
    assert cov["catalog"]["pct"] < 1.0, (
        f"Expected low coverage (<1.0%), got {cov['catalog']['pct']}%"
    )
    
    # Must contain low coverage or advisory warning
    warning_codes = [w["code"] for w in (res.warnings or [])]
    assert ("filter_key_not_extracted" in warning_codes or "low_precision_advisory" in warning_codes), (
        f"Expected coverage warning in {res.warnings}"
    )
