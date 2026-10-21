import asyncio
import json
import sys

sys.path.insert(0, ".")

from mcp import ClientSession
from mcp.client.sse import sse_client


async def main():
    async with sse_client("http://127.0.0.1:8001/sse") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools", len(tools.tools))
            result = await session.call_tool("describe_catalog", {})
            payload = json.loads(result.content[0].text)
            print("ok", payload.get("ok"))
            print("counts", payload.get("data", {}).get("counts"))
            print("spec keys", len(payload.get("data", {}).get("spec_keys", [])))
            result = await session.call_tool(
                "count_components",
                {"category": "resistors", "filters": {"resistance_ohm": {"gte": 1000}}},
            )
            payload = json.loads(result.content[0].text)
            print("resistor filter", payload.get("ok"), payload.get("total_matches"))


asyncio.run(main())
