import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = r"C:\Users\nithi\Downloads\Platfrom Scrapper"
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SCRIPT = os.path.join(ROOT, "scripts", "run_mcp.py")


async def main():
    params = StdioServerParameters(
        command=PY,
        args=[SCRIPT],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=ROOT,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("tools", len(tools.tools))
            result = await session.call_tool("describe_catalog", {})
            payload = json.loads(result.content[0].text)
            print("ok", payload.get("ok"), "keys", len(payload["data"]["spec_keys"]))
            result = await session.call_tool("count_components", {"category": "resistors"})
            print("resistors", json.loads(result.content[0].text).get("total_matches"))


asyncio.run(main())
