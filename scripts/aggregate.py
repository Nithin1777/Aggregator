"""
`aggregate` command launcher: start the web UI, the MCP server, or both.

Examples:
    aggregate                 # web on :8000 + MCP SSE on :8001 + tunnel
    aggregate --web           # web only
    aggregate --mcp           # MCP SSE on :8001 + aggregate-local tunnel
    aggregate --mcp --no-tunnel # local MCP SSE only
    aggregate --mcp --stdio   # MCP stdio (for MCP client configs)

The command is installed as `aggregate.cmd` in C:\\Users\\nithi\\.local\\bin,
which is already on PATH.
"""
from __future__ import annotations

import argparse
import os
import shutil
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
from scripts.managed_processes import ManagedProcesses

DEFAULT_TUNNEL_PROFILE = "aggregate-local"


def print_mcp_help() -> int:
    """Print transport-agnostic MCP connection instructions for people and agents."""
    print("")
    print("Aggregate MCP - connection guide")
    print("=" * 72)
    print("Tools: search_components, search_by_mpn, get_product, count_components,")
    print("       discover_specs, spec_values, describe_catalog, describe_spec,")
    print("       list_spec_keys, health")
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
    print("1. Discover unfamiliar categories/fields with describe_catalog/discover_specs.")
    print("2. Prefer category + filters/vendor_filters; keep model/brand text in query.")
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
    print("  Tunnel               -> starts automatically with aggregate/--mcp")
    print("  Local only           -> aggregate --mcp --no-tunnel")
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


def find_tunnel_client(explicit: str | None = None) -> Path:
    """Use the local release and its bundled companions; do not copy credentials."""
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise FileNotFoundError(f"Tunnel client not found: {candidate}")
    filename = "tunnel-client.exe" if os.name == "nt" else "tunnel-client"
    candidates = [BASE_DIR / filename, BASE_DIR / "tunnel-client" / filename]
    candidates += sorted(BASE_DIR.glob(f"tunnel-client-*/{filename}"), reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    installed = shutil.which(filename)
    if installed:
        return Path(installed).resolve()
    raise FileNotFoundError("Tunnel client not found. Put its release folder in the project root, "
                            "pass --tunnel-client PATH, or use --no-tunnel for local-only operation.")


def open_browser_later(url: str, delay: float = 1.6) -> None:
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def run_foreground(cmd: list[str]) -> int:
    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(BASE_DIR))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.call(cmd, cwd=str(BASE_DIR), env=env)


def run_web(host: str, port: int, open_page: bool) -> int:
    return run_services(host, web_port=port, open_page=open_page, tunnel=False)


def run_mcp(host: str, port: int, stdio: bool, **tunnel_options) -> int:
    if stdio:
        # stdio is a protocol channel: do not print anything before handing over.
        return run_foreground([PYTHON, RUN_MCP, "--transport", "stdio"])
    return run_services(host, mcp_port=port, **tunnel_options)


def run_both(host: str, web_port: int, mcp_port: int, open_page: bool, **tunnel_options) -> int:
    return run_services(host, web_port=web_port, mcp_port=mcp_port,
                        open_page=open_page, **tunnel_options)


def run_services(host: str, *, web_port: int | None = None, mcp_port: int | None = None,
                 open_page: bool = False, tunnel: bool = True,
                 tunnel_client: str | None = None,
                 tunnel_profile: str = DEFAULT_TUNNEL_PROFILE) -> int:
    try:
        client = find_tunnel_client(tunnel_client) if tunnel else None
        reused_ports = []
        with ManagedProcesses(BASE_DIR) as processes:
            for name, port, command, suffix in [
                ("web", web_port, [PYTHON, "-m", "uvicorn", "main:web_app"], ""),
                ("mcp", mcp_port, [PYTHON, RUN_MCP, "--transport", "sse"], "/sse"),
            ]:
                if port is None:
                    continue
                url = f"http://{public_host(host)}:{port}{suffix}"
                if port_in_use(host, port):
                    print(f"[{name}] already running at {url}")
                    reused_ports.append((name, port))
                else:
                    processes.start(command + ["--host", host, "--port", str(port)], name)
                print(f"[{name}] {url}", flush=True)
                if name == "web" and open_page:
                    open_browser_later(url)
            if client:
                processes.start([str(client), "run", "--profile", tunnel_profile],
                                "tunnel", cwd=client.parent)
                print(f"[tunnel] profile {tunnel_profile}; stops with Aggregate", flush=True)
            if not processes.children:
                return 0
            print("Press Ctrl+C to stop Aggregate and its tunnel.", flush=True)
            while True:
                for name, proc in processes.children:
                    code = proc.poll()
                    if code is not None:
                        print(f"[{name}] exited with code {code}", flush=True)
                        return code
                for name, port in reused_ports:
                    if not port_in_use(host, port):
                        print(f"[{name}] existing server stopped; stopping Aggregate's processes.", flush=True)
                        return 0
                time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nStopping Aggregate...", flush=True)
        return 0
    except OSError as exc:
        print(f"Could not start Aggregate: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    raw_args = sys.argv[1:]
    if "--mcp-help" in raw_args or (
        ("--help" in raw_args or "-h" in raw_args) and "--mcp" in raw_args
    ):
        return print_mcp_help()

    parser = argparse.ArgumentParser(
        prog="aggregate",
        description="Start the Aggregate web UI and/or MCP server with its managed tunnel.",
        epilog=(
            "MCP connection guide:  aggregate --help --mcp   (or aggregate --mcp-help)\n"
            "Examples:\n"
            "  aggregate                 web + MCP SSE + tunnel, opens browser\n"
            "  aggregate --web           web only\n"
            "  aggregate --mcp           MCP SSE + tunnel\n"
            "  aggregate --mcp --no-tunnel local MCP SSE only\n"
            "  aggregate --mcp --stdio   MCP stdio for a client"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--web", action="store_true", help="Start the web UI only.")
    target.add_argument("--mcp", action="store_true", help="Start MCP SSE and its tunnel (stdio stays local).")
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
    parser.add_argument("--no-tunnel", action="store_true",
                        help="Run local servers without the automatic tunnel.")
    parser.add_argument("--tunnel-profile", default=DEFAULT_TUNNEL_PROFILE,
                        help="Tunnel client profile (default: aggregate-local).")
    parser.add_argument("--tunnel-client", metavar="PATH",
                        help="Override the automatically discovered tunnel-client executable.")
    args = parser.parse_args()

    if args.mcp_help:
        return print_mcp_help()
    if args.stdio and not args.mcp:
        parser.error("--stdio is only valid with --mcp")
    if args.list_tools:
        return run_foreground([PYTHON, RUN_MCP, "--list-tools"])

    tunnel_options = dict(tunnel=not args.no_tunnel, tunnel_client=args.tunnel_client,
                          tunnel_profile=args.tunnel_profile)
    if args.web:
        return run_web(args.host, args.port, open_page=not args.no_open)
    if args.mcp:
        return run_mcp(args.host, args.mcp_port, stdio=args.stdio, **tunnel_options)
    return run_both(args.host, args.port, args.mcp_port, open_page=not args.no_open,
                    **tunnel_options)


if __name__ == "__main__":
    raise SystemExit(main())
