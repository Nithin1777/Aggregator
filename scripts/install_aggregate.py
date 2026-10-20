"""
Install the `aggregate` command into %USERPROFILE%\\.local\\bin.

The generated aggregate.cmd uses this checkout's .venv Python and launcher, so
it works from any directory. `%USERPROFILE%\\.local\\bin` is on PATH on this
machine; the script prints a warning if it is not.

Usage:
    python scripts/install_aggregate.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = Path.home() / ".local" / "bin" / "aggregate.cmd"


def main() -> int:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        print(f"Virtualenv Python not found: {python}", file=sys.stderr)
        return 1

    content = f"""@echo off
setlocal
set "AGG_ROOT={ROOT}"
if not exist "%AGG_ROOT%\\.venv\\Scripts\\python.exe" (
  echo Aggregate virtualenv not found at "%AGG_ROOT%\\.venv\\Scripts\\python.exe".
  echo Update AGG_ROOT in this launcher or recreate the venv.
  exit /b 1
)
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%AGG_ROOT%\\.venv\\Scripts\\python.exe" "%AGG_ROOT%\\scripts\\aggregate.py" %*
exit /b %ERRORLEVEL%
"""
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    try:
        TARGET.write_text(content, encoding="utf-8", newline="\r\n")
    except OSError as exc:
        print(
            f"Could not write {TARGET}: {exc}\n"
            f"Run this installer from a normal terminal with write access to your "
            f"home directory.",
            file=sys.stderr,
        )
        return 1
    print(f"Installed: {TARGET}")

    path_parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    if str(TARGET.parent) not in path_parts:
        print(
            f"Note: {TARGET.parent} is not on PATH. Add it to your user PATH, then "
            f"open a new terminal.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
