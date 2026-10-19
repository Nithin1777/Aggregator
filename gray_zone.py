"""
Production Gray-Zone Telemetry, Active Audit Logging, and Anomaly Detection.
Captures ambiguous merges, price disparities, numeric divergences, and vetoes in the wild.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console
from rich.table import Table

from config import settings
from models import UnifiedProduct

console = Console()

def get_db():
    conn = sqlite3.connect(str(settings.db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn

def extract_numeric_tokens(title: str) -> set:
    """Extracts significant numeric values (e.g. 5, 30, 38, 50, 75, 1000)."""
    tokens = re.findall(r"\b\d+(?:\.\d+)?\b", title.lower())
    # Exclude trivial non-discriminating single-digit numbers like 1, 2
    return {t for t in tokens if len(t) > 1 or float(t) >= 3.0}

def evaluate_gray_zone_risk(
    p1: UnifiedProduct,
    p2: UnifiedProduct,
    is_match: bool,
    score: float,
    reason: str,
    query: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Analyzes a matching decision between two vendor products for operational risk.
    Returns a telemetry dictionary if the decision falls into the gray zone or triggers alerts.
    """
    flags = []
    risk_level = "low"

    t1_lower = p1.title.lower()
    t2_lower = p2.title.lower()

    # 1. Price Disparity Check (Merged items with vastly different prices)
    if is_match and p1.price > 0 and p2.price > 0:
        ratio = max(p1.price, p2.price) / min(p1.price, p2.price)
        if ratio >= 3.0:
            flags.append(f"EXTREME_PRICE_DISPARITY_{ratio:.1f}X")
            risk_level = "high"
        elif ratio >= 2.0:
            flags.append(f"MODERATE_PRICE_DISPARITY_{ratio:.1f}X")
            if risk_level != "high":
                risk_level = "medium"

    # 2. Kit / Educational Project Keyword Check
    kit_pattern = re.compile(r"\b(kit|diy|project|combo|make\s+a)\b", re.IGNORECASE)
    has_kit1 = bool(kit_pattern.search(p1.title))
    has_kit2 = bool(kit_pattern.search(p2.title))
    if has_kit1 != has_kit2:
        flags.append("KIT_VS_STANDALONE_MISMATCH")
        if is_match:
            risk_level = "high"
        elif risk_level != "high":
            risk_level = "medium"

    # 3. Numeric Token Divergence Check
    if is_match:
        nums1 = extract_numeric_tokens(p1.title)
        nums2 = extract_numeric_tokens(p2.title)
        # Disjoint numbers that indicate different models or specs
        diff = nums1.symmetric_difference(nums2)
        if diff:
            flags.append(f"NUMERIC_DIVERGENCE({','.join(list(diff)[:3])})")
            if risk_level != "high":
                risk_level = "medium"

    # 4. Asymmetric MPN (One has MPN, one does not, but merged)
    has_mpn1 = bool(p1.mpn)
    has_mpn2 = bool(p2.mpn)
    if is_match and (has_mpn1 != has_mpn2):
        flags.append("ASYMMETRIC_MPN_MERGE")
        if risk_level != "high":
            risk_level = "medium"

    # 5. Fuzzy Score in Ambiguous Gray Zone
    if 0.45 <= score <= 0.85:
        flags.append(f"SCORE_IN_GRAY_ZONE({score:.2f})")
        if risk_level != "high":
            risk_level = "medium"

    # 6. Hard Veto Triggered
    if "Hard Veto" in reason:
        flags.append("HARD_VETO_BLOCKED")
        # Logged for telemetry, but risk is low because engine prevented it
        if not flags or risk_level == "low":
            risk_level = "low"

    # If no flags triggered, this was a clear, unambiguous decision (skip logging)
    if not flags:
        return None

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query or "",
        "vendor_a": p1.vendor_name,
        "title_a": p1.title,
        "mpn_a": p1.mpn,
        "price_a": p1.price,
        "vendor_b": p2.vendor_name,
        "title_b": p2.title,
        "mpn_b": p2.mpn,
        "price_b": p2.price,
        "score": round(score, 3),
        "decision": "merged" if is_match else "rejected",
        "risk_level": risk_level,
        "flags": flags,
        "reason": reason
    }


def record_gray_zone_entry(entry: Dict[str, Any]):
    """Persists a gray-zone record to both JSONL file and SQLite table."""
    # 1. JSONL Append
    try:
        settings.matcher_audit_log_path.parent.mkdir(exist_ok=True)
        with open(settings.matcher_audit_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        console.print(f"[dim]Failed to write JSONL gray zone log: {e}[/dim]")

    # 2. SQLite Insert
    try:
        conn = get_db()
        with conn:
            conn.execute("""
                INSERT INTO matcher_gray_zone (
                    query, vendor_a, title_a, mpn_a, price_a,
                    vendor_b, title_b, mpn_b, price_b,
                    score, decision, risk_level, flags, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                entry.get("query", ""),
                entry["vendor_a"],
                entry["title_a"],
                entry.get("mpn_a"),
                entry.get("price_a", 0.0),
                entry["vendor_b"],
                entry["title_b"],
                entry.get("mpn_b"),
                entry.get("price_b", 0.0),
                entry["score"],
                entry["decision"],
                entry["risk_level"],
                json.dumps(entry["flags"]),
                entry["reason"]
            ))
        conn.close()
    except Exception as e:
        console.print(f"[dim]Failed to insert gray zone DB row: {e}[/dim]")


def get_gray_zone_records(limit: int = 50, high_risk_only: bool = False) -> List[Dict[str, Any]]:
    """Fetches recent gray zone entries from SQLite database."""
    conn = get_db()
    try:
        query = "SELECT * FROM matcher_gray_zone"
        params = []
        if high_risk_only:
            query += " WHERE risk_level = 'high'"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["flags"] = json.loads(d["flags"]) if d["flags"] else []
            except Exception:
                d["flags"] = []
            result.append(d)
        return result
    finally:
        conn.close()


def print_gray_zone_table(limit: int = 30, high_risk_only: bool = False):
    """Renders a beautiful Rich table summarizing the gray-zone log for humans."""
    records = get_gray_zone_records(limit=limit, high_risk_only=high_risk_only)

    title_suffix = " (HIGH RISK ONLY)" if high_risk_only else ""
    console.print(f"\n[bold cyan]================================================================[/bold cyan]")
    console.print(f"[bold white]   MATCHER GRAY-ZONE TELEMETRY LOG{title_suffix}   [/bold white]")
    console.print(f"[bold cyan]================================================================[/bold cyan]\n")

    if not records:
        console.print("[dim]No gray-zone entries recorded yet. Run queries or ingestion to populate.[/dim]\n")
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Risk", justify="center")
    table.add_column("Decision", justify="center")
    table.add_column("Score", justify="right")
    table.add_column("Item A (Vendor / Price)", style="cyan")
    table.add_column("Item B (Vendor / Price)", style="cyan")
    table.add_column("Triggered Flags", style="bold yellow")

    for r in records:
        risk_style = {
            "high": "[bold red]HIGH[/bold red]",
            "medium": "[yellow]MED[/yellow]",
            "low": "[dim]LOW[/dim]"
        }.get(r["risk_level"], r["risk_level"])

        dec_style = "[bold green]MERGED[/bold green]" if r["decision"] == "merged" else "[bold red]REJECTED[/bold red]"
        score_val = f"{r['score']:.2f}"

        item_a = f"{r['title_a'][:38]}...\n[dim]({r['vendor_a']} - Rs.{r['price_a']:.0f})[/dim]"
        item_b = f"{r['title_b'][:38]}...\n[dim]({r['vendor_b']} - Rs.{r['price_b']:.0f})[/dim]"
        flags_str = ", ".join(r["flags"][:2])

        table.add_row(
            str(r["id"]),
            risk_style,
            dec_style,
            score_val,
            item_a,
            item_b,
            flags_str
        )

    console.print(table)
    console.print(f"\nTotal entries displayed: [bold yellow]{len(records)}[/bold yellow] (use [cyan]python main.py gray-zone --all[/cyan] to view more)\n")
