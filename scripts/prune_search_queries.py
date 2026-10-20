"""
Prunes the search-query log.

Removes one-off and too-short queries so the "popular searches" hints stay
useful (a query needs at least 3 characters and 2 hits to be shown).
"""
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

from config import settings  # noqa: E402


def main() -> int:
    conn = sqlite3.connect(str(settings.db_path))
    try:
        conn.execute("PRAGMA temp_store = MEMORY;")
        before = conn.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0]
        conn.execute("DELETE FROM search_queries WHERE LENGTH(query) < 3 OR count < 2;")
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM search_queries").fetchone()[0]
        remaining = conn.execute(
            "SELECT query, count FROM search_queries ORDER BY count DESC LIMIT 20;"
        ).fetchall()
    finally:
        conn.close()

    print(f"search_queries: {before} -> {after}")
    for query, count in remaining:
        print(f"  {count:>4}  {query}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
