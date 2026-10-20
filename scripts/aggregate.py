"""
`aggregate` command launcher: start the web UI, the MCP server, or both.

Examples:
    aggregate                 # web on :8000 + MCP SSE on :8001
    aggregate --web           # web only
    aggregate --mcp           # MCP SSE only (http://127.0.0.1:8001/sse)
    aggregate --mcp --stdio   # MCP stdio (for MCP client configs)

The command is installed as `aggregate.cmd` in C:\\Users\\nithi\\.local\\bin,
which is already on PATH.
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
VENV_PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"
PYTHON = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
RUN_MCP = str(BASE_DIR / "scripts" / "run_mcp.py")


def print_mcp_help() -> int:
    """Print transport-agnostic MCP connection instructions for people and agents."""
    print("")
    print("Aggregate MCP - connection guide")
    print("=" * 72)
    print("Tools: search_components, search_by_mpn, get_product, count_components,")
    print("       describe_catalog, describe_spec, list_spec_keys, health")
    print("")
    print("TRANSPORT 1 - STDIO (recommended for local agents)")
    print("-" * 72)
    print("Let your MCP client spawn the server. If the `aggregate` command is")
    print("on PATH, configure the client as:")
    print("")
    print("  type: stdio")
    print("  command: aggregate")
    print('  args: ["--mcp", "--stdio"]')
    print("")
    print("If `aggregate` is not on PATH, run the project's setup helper from the")
    print("project root; it writes the correct client configuration for you:")
    print("")
    print("  python scripts/setup_mcp.py claude-code")
    print("  python scripts/setup_mcp.py cursor")
    print("  python scripts/setup_mcp.py vscode")
    print("")
    print("Never run stdio manually in a shared terminal: stdout is the protocol.")
    print("")
    print("TRANSPORT 2 - SSE (long-running server)")
    print("-" * 72)
    print("Start the server in a terminal:")
    print("  aggregate --mcp")
    print("")
    print("Connect the client to:")
    print("  http://127.0.0.1:8001/sse")
    print("")
    print("  type: sse")
    print("  url: http://127.0.0.1:8001/sse")
    print("")
    print("AGENT WORKFLOW")
    print("-" * 72)
    print("1. Call describe_catalog first to learn categories and spec keys.")
    print("2. Then search_components(query=..., limit=10) or search_by_mpn(mpn=...).")
    print("3. Results are vendor listings, not de-duplicated parts. Compare by mpn.")
    print("4. Prices are INR and tax-inclusive.")
    print("5. Pass refresh=False for fast cached reads; refresh=True verifies live")
    print("   price/stock with the vendors (slower).")
    print("6. search_components returns next_cursor; pass it back for the next page.")
    print("")
    print("VERIFY")
    print("-" * 72)
    print("  aggregate --list-tools")
    print("  aggregate --mcp                 # SSE on :8001")
    print("  aggregate --mcp --stdio         # stdio handshake for a client")
    print("")
    print("TROUBLESHOOTING")
    print("-" * 72)
    print("  Port 8001 busy       -> aggregate --mcp --mcp-port 8010")
    print("  Import errors        -> run from the project root with the project Python")
    print("  Empty catalog        -> python main.py status")
    print("  Web UI too           -> aggregate            (web :8000 + MCP SSE :8001)")
    print("")
    return 0


def public_host(host: str) -> str:
    """A host that can be opened/printed from this machine."""
    return "127.0.0.1" if host in ("0.0.0.0", "::", "") else host


def port_in_use(host: str, port: int) -> bool:
    """True when something is already listening on host:port."""
    try:
        with socket.create_connection((public_host(host), port), timeout=0.4):
            return True
    except OSError:
        return False


def start(cmd: list[str], name: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(BASE_DIR))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    print(f"[{name}] starting...", flush=True)
    return subprocess.Popen(cmd, cwd=str(BASE_DIR), env=env)


def stop(proc: subprocess.Popen, name: str) -> None:
    if proc.poll() is not None:
        return
    print(f"[{name}] stopping...", flush=True)
    try:
        proc.terminate()
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass


def open_browser_later(url: str, delay: float = 1.6) -> None:
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def run_foreground(cmd: list[str]) -> int:
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(BASE_DIR))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.call(cmd, cwd=str(BASE_DIR), env=env)


def run_web(host: str, port: int, open_page: bool) -> int:
    url = f"http://{public_host(host)}:{port}"
    if port_in_use(host, port):
        print(f"Web UI is already running at {url}")
        if open_page:
            webbrowser.open(url)
        return 0
    if open_page:
        open_browser_later(url)
    return run_foreground(
        [PYTHON, "-m", "uvicorn", "main:web_app", "--host", host, "--port", str(port)]
    )


def run_mcp(host: str, port: int, stdio: bool) -> int:
    if stdio:
        # stdio is a protocol channel: do not print anything before handing over.
        return run_foreground([PYTHON, RUN_MCP, "--transport", "stdio"])
    if port_in_use(host, port):
        print(f"MCP SSE is already running at http://{public_host(host)}:{port}/sse")
        return 0
    print(f"Starting MCP SSE on http://{public_host(host)}:{port}/sse")
    return run_foreground(
        [PYTHON, RUN_MCP, "--transport", "sse", "--host", host, "--port", str(port)]
    )


def run_both(host: str, web_port: int, mcp_port: int, open_page: bool) -> int:
    procs: list[tuple[str, subprocess.Popen]] = []

    web_url = f"http://{public_host(host)}:{web_port}"
    mcp_url = f"http://{public_host(host)}:{mcp_port}/sse"
    if port_in_use(host, web_port):
        print(f"[web] already running at {web_url}")
        if open_page:
            webbrowser.open(web_url)
    else:
        procs.append((
            "web",
            start(
                [PYTHON, "-m", "uvicorn", "main:web_app", "--host", host, "--port", str(web_port)],
                "web",
            ),
        ))
        if open_page:
            open_browser_later(web_url)

    if port_in_use(host, mcp_port):
        print(f"[mcp] already running at {mcp_url}")
    else:
        procs.append((
            "mcp",
            start(
                [PYTHON, RUN_MCP, "--transport", "sse", "--host", host, "--port", str(mcp_port)],
                "mcp",
            ),
        ))

    if not procs:
        print(f"Web: {web_url}")
        print(f"MCP: {mcp_url}")
        return 0

    print("")
    print(f"  Web UI   {web_url}")
    print(f"  MCP SSE  {mcp_url}")
    print("  Press Ctrl+C to stop everything.")
    print("")

    try:
        while True:
            for name, proc in procs:
                code = proc.poll()
                if code is not None:
                    print(f"[{name}] exited with code {code}")
                    for other_name, other in procs:
                        if other is not proc:
                            stop(other, other_name)
                    return code
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nStopping Aggregate...", flush=True)
        for name, proc in procs:
            stop(proc, name)
        return 0


def main() -> int:
    raw_args = sys.argv[1:]
    if "--mcp-help" in raw_args or (
        ("--help" in raw_args or "-h" in raw_args) and "--mcp" in raw_args
    ):
        return print_mcp_help()

    parser = argparse.ArgumentParser(
        prog="aggregate",
        description="Start the Aggregate web UI and/or MCP server.",
        epilog=(
            "MCP connection guide:  aggregate --help --mcp   (or aggregate --mcp-help)\n"
            "Examples:\n"
            "  aggregate                 web + MCP SSE, opens browser\n"
            "  aggregate --web           web only\n"
            "  aggregate --mcp           MCP SSE only\n"
            "  aggregate --mcp --stdio   MCP stdio for a client"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--web", action="store_true", help="Start the web UI only.")
    target.add_argument("--mcp", action="store_true", help="Start the MCP server only.")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="With --mcp: use stdio transport (for MCP client configs) instead of SSE.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Web port (default: 8000).")
    parser.add_argument(
        "--mcp-port", type=int, default=8001, help="MCP SSE port (default: 8001)."
    )
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open the browser automatically."
    )
    parser.add_argument(
        "--list-tools", action="store_true", help="List the MCP tools and exit."
    )
    parser.add_argument(
        "--mcp-help",
        action="store_true",
        help="Show MCP connection instructions and exit.",
    )
    args = parser.parse_args()

    if args.mcp_help:
        return print_mcp_help()
    if args.stdio and not args.mcp:
        parser.error("--stdio is only valid with --mcp")
    if args.list_tools:
        return run_foreground([PYTHON, RUN_MCP, "--list-tools"])

    if args.web:
        return run_web(args.host, args.port, open_page=not args.no_open)
    if args.mcp:
        return run_mcp(args.host, args.mcp_port, stdio=args.stdio)
    return run_both(args.host, args.port, args.mcp_port, open_page=not args.no_open)


if __name__ == "__main__":
    raise SystemExit(main())
