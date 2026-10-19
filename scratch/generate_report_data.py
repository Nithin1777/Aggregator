import asyncio
import json
from mcp_server.server import (
    describe_catalog,
    describe_spec,
    count_components,
    search_components,
    get_product,
    list_spec_keys,
    normalize_scalar,
    mcp,
    SERVER_VERSION
)

async def main():
    report_data = {}

    # 1. Wire fix confirmation
    tools = await mcp.list_tools()
    c_list, _ = await mcp.call_tool("list_spec_keys", {})
    report_data["wire_blocks_count"] = len(c_list)
    report_data["tools_count"] = len(tools)
    report_data["output_schemas_present"] = all(t.outputSchema is not None for t in tools)

    # 2. Envelope: Success Search and Error
    s_success = search_components(filters={"voltage": "12", "rpm": {"gte": 190, "lte": 210}}, limit=2)
    s_error = search_components(filters={"unknown_spec_key": "val"})
    report_data["envelope_success"] = s_success.model_dump()
    report_data["envelope_error"] = s_error.model_dump()

    # 3. unknown / absent / null examples
    # Example A: filter_matched (normal results with null for unextracted spec)
    # Example B: filter_key_not_extracted (key with low coverage e.g. current_a)
    # Example C: filter_value_not_present (key exists, but value matches 0 rows)
    ex_matched = search_components(filters={"voltage": "12"}, limit=1).model_dump()
    ex_low_cov = search_components(filters={"current_a": 1.5}, limit=1).model_dump()
    ex_val_not_present = search_components(filters={"chemistry": "unobtainium_chem"}, limit=1).model_dump()
    report_data["unknown_absent_examples"] = {
        "matched": ex_matched,
        "not_extracted_low_cov": ex_low_cov,
        "val_not_present": ex_val_not_present,
    }

    # 4. Normalization: 3 input forms normalizing to same canonical value
    norm_examples = {
        "voltage": [
            ("24V", normalize_scalar("voltage", "24V", "enum", "V")),
            ("24 v", normalize_scalar("voltage", "24 v", "enum", "V")),
            ("24VDC", normalize_scalar("voltage", "24VDC", "enum", "V")),
        ],
        "capacity_mah": [
            ("5200 mAh", normalize_scalar("capacity_mah", "5200 mAh", "integer", "mAh")),
            ("5.2Ah", normalize_scalar("capacity_mah", "5.2Ah", "integer", "mAh")),
            ("5200mah", normalize_scalar("capacity_mah", "5200mah", "integer", "mAh")),
        ],
        "cells": [
            ("4S", normalize_scalar("cells", "4S", "enum", None)),
            ("4 s", normalize_scalar("cells", "4 s", "enum", None)),
            ("4s lipo", normalize_scalar("cells", "4s lipo", "enum", None)),
        ],
    }
    report_data["normalization_examples"] = norm_examples

    # 5. New tools outputs
    cat = describe_catalog().model_dump()
    spec_rpm = describe_spec(key="rpm").model_dump()
    # pick one real product id from motor search
    sample_pid = s_success.data["results"][0]["product_id"] if s_success.data.get("results") else 44859
    prod = get_product(product_id=sample_pid).model_dump()

    report_data["describe_catalog"] = cat
    report_data["describe_spec_rpm"] = spec_rpm
    report_data["get_product_sample"] = prod

    # 6. Call chain for '12v 200rpm motor'
    # Step 1: describe_catalog
    step1 = describe_catalog().model_dump()
    # Step 2: describe_spec('rpm')
    step2 = describe_spec(key="rpm").model_dump()
    # Step 3: count_components
    step3 = count_components(filters={"voltage": "12", "rpm": {"eq": 200, "tolerance": 0.05}}, query="motor").model_dump()
    # Step 4: search_components
    step4 = search_components(filters={"voltage": "12", "rpm": {"eq": 200, "tolerance": 0.05}}, query="motor", limit=2).model_dump()
    chosen_id = step4["data"]["results"][0]["product_id"] if step4["data"]["results"] else sample_pid
    # Step 5: get_product
    step5 = get_product(product_id=chosen_id).model_dump()

    report_data["call_chain"] = {
        "step1_describe_catalog_summary": {
            "total_products": step1["data"]["total_products"],
            "spec_keys_advertised": [k["key"] for k in step1["data"]["spec_keys"]],
        },
        "step2_describe_spec_rpm": {
            "key": step2["data"]["key"],
            "status": step2["data"]["status"],
            "coverage_count": step2["data"]["coverage_count"],
            "range": step2["data"]["range"],
        },
        "step3_count_components": {
            "total_matches": step3["total_matches"],
            "filters_applied": step3["filters_applied"],
            "coverage": step3["coverage"],
        },
        "step4_search_components": {
            "total_matches": step4["total_matches"],
            "returned": step4["returned"],
            "truncated": step4["truncated"],
            "results_sample": step4["data"]["results"][:1],
        },
        "step5_get_product": {
            "product_id": step5["data"]["product_id"],
            "title": step5["data"]["title"],
            "price": step5["data"]["price"],
            "vendor_offers_count": len(step5["data"]["vendor_offers"]),
            "extracted_specs": step5["data"]["extracted_specs"],
        }
    }

    with open("scratch/round4_report_data.json", "w", encoding="utf-8") as f:
        json.dump(report_data, f, indent=2)
    print("Report data generated successfully in scratch/round4_report_data.json")

if __name__ == "__main__":
    asyncio.run(main())
