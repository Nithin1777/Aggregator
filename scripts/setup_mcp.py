"""
Registers the Aggregate MCP server with the MCP client of your choice.

    python scripts/setup_mcp.py claude-code        # claude mcp add (user scope)
    python scripts/setup_mcp.py claude-desktop     # %APPDATA%\\Claude\\claude_desktop_config.json
    python scripts/setup_mcp.py cursor             # .cursor/mcp.json
    python scripts/setup_mcp.json vscode            # .vscode/mcp.json
    python scripts/setup_mcp.py windsurf           # ~/.codeium/windsurf/mcp_config.json
    python scripts/setup_mcp.py show               # print the config, change nothing

The stdio server is started through `scripts/run_mcp.py`, which pins the
project root as the working directory - some clients do not send one, and the
server has to start there to import the project modules.

Existing configuration files are merged, never overwritten. Run with --dry-run
to see what would change.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

BASE_DIR = Path(__file__).resolve().parents[1]
VENV_PYTHON = BASE_DIR / ".venv" / "Scripts" / "python.exe"
LAUNCHER = BASE_DIR / "scripts" / "run_mcp.py"
SERVER_NAME = "aggregate"

if sys.platform == "win32":
    PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable))
else:
    PYTHON = str(VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable))


def server_entry() -> Dict[str, Any]:
    """The stdio server definition every client understands."""
    return {
        "command": PYTHON,
        "args": [str(LAUNCHER)],
        "cwd": str(BASE_DIR),
        "env": {"PYTHONUNBUFFERED": "1"},
    }


# ---------------------------------------------------------------------------
# Client targets
# ---------------------------------------------------------------------------

def target_claude_code(scope: str = "project") -> Dict[str, Any]:
    """Claude Code registers servers through its own CLI, not a JSON file."""
    return {
        "kind": "cli",
        "command": [
            "claude", "mcp", "add", SERVER_NAME,
            "--scope", scope,
            "--", PYTHON, str(LAUNCHER),
        ],
        "note": f"Verify with: claude mcp list   (scope: {scope})",
    }


def target_claude_desktop() -> Dict[str, Any]:
    path = Path(os.environ.get("APPDATA", Path.home())) / "Claude" / "claude_desktop_config.json"
    return {"kind": "json", "path": path, "key": "mcpServers", "wrap": False}


def target_cursor() -> Dict[str, Any]:
    return {"kind": "json", "path": BASE_DIR / ".cursor" / "mcp.json", "key": "mcpServers", "wrap": False}


def target_vscode() -> Dict[str, Any]:
    return {"kind": "json", "path": BASE_DIR / ".vscode" / "mcp.json", "key": "servers", "wrap": False}


def target_windsurf() -> Dict[str, Any]:
    path = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    return {"kind": "json", "path": path, "key": "mcpServers", "wrap": False}


def target_continue() -> Dict[str, Any]:
    return {"kind": "json", "path": Path.home() / ".continue" / "config.yaml", "key": None, "wrap": False}


TARGETS = {
    "claude-code": target_claude_code,
    "claude-desktop": target_claude_desktop,
    "cursor": target_cursor,
    "vscode": target_vscode,
    "windsurf": target_windsurf,
    "continue": target_continue,
}


# ---------------------------------------------------------------------------
# JSON writing
# ---------------------------------------------------------------------------

def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def register_json_target(target: Dict[str, Any], dry_run: bool) -> str:
    path: Path = target["path"]
    key: str = target["key"]
    existing = load_json(path)

    servers = existing.setdefault(key, {})
    if not isinstance(servers, dict):
        servers = {}
        existing[key] = servers

    previous = servers.get(SERVER_NAME)
    servers[SERVER_NAME] = server_entry()

    if dry_run:
        return f"would update {path}"

    write_json(path, existing)
    verb = "updated" if previous else "added"
    return f"{verb} {SERVER_NAME} in {path}"


def register_cli_target(target: Dict[str, Any], dry_run: bool) -> str:
    command = target["command"]
    if shutil.which(command[0]) is None:
        raise SystemExit(
            f"'{command[0]}' is not on PATH. Install Claude Code first, or use "
            "'python scripts/setup_mcp.py cursor' instead."
        )
    if dry_run:
        return "would run: " + " ".join(command)
    import subprocess

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        message = (result.stderr.strip() or result.stdout.strip())
        raise SystemExit(
            f"command failed: {message}\n"
            "The user scope writes to ~/.claude.json. If that file is protected, "
            "use the project scope instead:\n"
            "    python scripts/setup_mcp.py claude-code --scope project"
        )
    return (result.stdout.strip() or result.stderr.strip() or "done") + f"  ({target['note']})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "client",
        nargs="?",
        choices=sorted(TARGETS.keys()) + ["show"],
        help="Which client to register with.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing anything.",
    )
    parser.add_argument(
        "--scope",
        choices=["project", "user", "local"],
        default="project",
        help="Claude Code scope (default: project - writes .mcp.json in the repo).",
    )
    args = parser.parse_args()

    if not args.client:
        parser.print_help()
        return 1

    if args.client == "show":
        print("Aggregate MCP server (stdio):\n")
        print(json.dumps({SERVER_NAME: server_entry()}, indent=2))
        print("\nProject root :", BASE_DIR)
        print("Python       :", PYTHON)
        print("Launcher     :", LAUNCHER)
        print("\nFor a remote client use the SSE transport:")
        print(f"  {PYTHON} {LAUNCHER} --transport sse --host 0.0.0.0 --port 8001")
        print("  then point the client at http://<host>:8001/sse")
        return 0

    if args.client == "claude-code":
        target = target_claude_code(args.scope)
    else:
        target = TARGETS[args.client]()
    if target["kind"] == "cli":
        message = register_cli_target(target, args.dry_run)
    else:
        message = register_json_target(target, args.dry_run)

    print(message)
    print("\nRestart the client so it picks the server up.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
