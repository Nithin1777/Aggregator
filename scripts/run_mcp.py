"""
Launcher for the Aggregate MCP server.

MCP clients spawn the server as a subprocess and talk to it over stdio, so the
server has to start in the project root (otherwise `import db` fails). Some
clients do not pass a working directory, so this launcher fixes that for them:

    "command": "C:\\path\\to\\python.exe",
    "args": ["C:\\path\\to\\project\\scripts\\run_mcp.py"]

Usage:
    python scripts/run_mcp.py                 # stdio (default, for local clients)
    python scripts/run_mcp.py --transport sse  # SSE on http://127.0.0.1:8001/sse
    python scripts/run_mcp.py --list-tools     # print the exposed tools and exit
"""
import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport mode: stdio (default, for local clients) or sse (remote)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="SSE host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8001, help="SSE port (default: 8001)")
    parser.add_argument(
        "--list-tools",
        action="store_true",
        help="Print the tools this server exposes and exit.",
    )
    args = parser.parse_args()

    # The server imports `db`, `config`, `models` from the project root.
    os.chdir(BASE_DIR)
    sys.path.insert(0, str(BASE_DIR))

    if args.list_tools:
        from mcp_server.server import mcp

        tools = asyncio_run(mcp.list_tools())
        print(f"{BASE_DIR.name} MCP server - {len(tools)} tools")
        for tool in tools:
            first = (tool.description or "").strip().split("\n")[0]
            print(f"  {tool.name:<22} {first}")
        return 0

    from mcp_server.server import mcp

    if args.transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        print(
            f"Starting Aggregate MCP server (SSE) on http://{args.host}:{args.port}/sse",
            file=sys.stderr,
        )
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
    return 0


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


if __name__ == "__main__":
    raise SystemExit(main())
