import asyncio
import json
from pathlib import Path
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from mcp_server.server import mcp

async def test_all_tools():
    print("=" * 60)
    print("VERIFYING 6 TOOLS WIRE-LEVEL CONTRACT & ENVELOPES")
    print("=" * 60)

    tools = await mcp.list_tools()
    print(f"Total tools discovered: {len(tools)}")
    assert len(tools) == 6

    for t in tools:
        assert t.outputSchema is not None, f"Tool {t.name} missing outputSchema"
        print(f"Tool '{t.name}': outputSchema is PRESENT ({len(t.outputSchema.get('properties', {}))} properties)")

    # Test 1: describe_catalog
    c1, m1 = await mcp.call_tool("describe_catalog", {})
    assert len(c1) == 1, f"describe_catalog returned {len(c1)} content blocks, expected 1"
    doc1 = json.loads(c1[0].text)
    assert doc1["ok"] is True
    print("\n[describe_catalog] Content blocks: 1. Total products:", doc1["data"]["total_products"])
    print("Spec keys count:", len(doc1["data"]["spec_keys"]))

    # Test 2: describe_spec
    c2, m2 = await mcp.call_tool("describe_spec", {"key": "rpm"})
    assert len(c2) == 1
    doc2 = json.loads(c2[0].text)
    assert doc2["ok"] is True
    print("\n[describe_spec('rpm')] Content blocks: 1. Status:", doc2["data"]["status"], "Count:", doc2["data"]["coverage_count"])

    # Test 3: count_components
    c3, m3 = await mcp.call_tool("count_components", {
        "filters": {"voltage": "12", "rpm": {"gte": 190, "lte": 210}},
        "query": "motor"
    })
    assert len(c3) == 1
    doc3 = json.loads(c3[0].text)
    assert doc3["ok"] is True
    print("\n[count_components] Content blocks: 1. Matches:", doc3["total_matches"], "Filters applied:", doc3["filters_applied"])

    # Test 4: search_components
    c4, m4 = await mcp.call_tool("search_components", {
        "filters": {"voltage": "12", "rpm": {"gte": 190, "lte": 210}},
        "query": "motor",
        "limit": 3
    })
    assert len(c4) == 1
    doc4 = json.loads(c4[0].text)
    assert doc4["ok"] is True
    print("\n[search_components] Content blocks: 1. Matches:", doc4["total_matches"], "Returned:", doc4["returned"], "Truncated:", doc4["truncated"])
    if doc4["data"]["results"]:
        sample_prod = doc4["data"]["results"][0]
        sample_pid = sample_prod["product_id"]
        print("First result title:", sample_prod["title"], "PID:", sample_pid)
        print("Sample product all specs keys:", list(sample_prod["specs"].keys()))
        assert "voltage" in sample_prod["specs"] and "rpm" in sample_prod["specs"]
        assert sample_prod["specs"]["voltage"] == "12"
        # Test 5: get_product on that product id
        c5, m5 = await mcp.call_tool("get_product", {"product_id": sample_pid})
        assert len(c5) == 1
        doc5 = json.loads(c5[0].text)
        assert doc5["ok"] is True
        print("\n[get_product] Content blocks: 1. Title:", doc5["data"]["title"], "Price:", doc5["data"]["price"])
        print("Vendor offers count:", len(doc5["data"]["vendor_offers"]))

    # Test 6: list_spec_keys (backward compat)
    c6, m6 = await mcp.call_tool("list_spec_keys", {})
    assert len(c6) == 1, f"list_spec_keys returned {len(c6)} content blocks, expected 1"
    doc6 = json.loads(c6[0].text)
    assert doc6["ok"] is True
    assert "spec_keys" in doc6["data"]
    print("\n[list_spec_keys] Content blocks: 1. Array length:", len(doc6["data"]["spec_keys"]))

    print("\n" + "=" * 60)
    print("ALL 6 TOOLS VERIFIED: EXACTLY 1 CONTENT BLOCK PER CALL!")
    print("=" * 60)

asyncio.run(test_all_tools())
