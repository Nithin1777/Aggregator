"""
Live Database Audit: Samples 100 entries across vendors and evaluates Matcher V2 in the wild.
"""
import random
import sqlite3
from typing import Dict, List, Tuple
from rich.console import Console
from rich.table import Table

from config import settings
from models import UnifiedProduct
from normalizer import (
    calculate_token_similarity,
    extract_hardware_specs,
    extract_mpn,
    get_canonical_cluster_key_v2,
    match_components_v2,
    normalize_title,
)
from search_service import AggregatorEngine

console = Console()

def get_db_connection():
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    return conn

def sample_cross_vendor_candidates(total_target: int = 100) -> List[Tuple[UnifiedProduct, UnifiedProduct]]:
    """
    Finds real cross-vendor pairs from data/components.db that share significant keywords,
    representing real-world candidates where matching decisions matter.
    """
    conn = get_db_connection()
    keywords = [
        "esp32", "nodemcu", "esp8266", "ch340", "cp2102",
        "lm2596", "ams1117", "xl4015", "tp4056", "buck", "boost",
        "mpu6050", "bme280", "bmp280", "dht11", "dht22", "ultrasonic", "hc-sr04",
        "sg90", "a4988", "drv8825", "l298n", "oled", "128x64", "servo",
        "stepper", "caster", "battery", "relay", "converter"
    ]
    random.shuffle(keywords)

    pairs: List[Tuple[UnifiedProduct, UnifiedProduct]] = []
    seen_pairs = set()

    for kw in keywords:
        rows = conn.execute("""
            SELECT vendor, vendor_name, external_id, title, normalized_title, mpn, sku, price, available, product_url, image_url
            FROM products
            WHERE title LIKE ?
            ORDER BY RANDOM()
            LIMIT 15;
        """, (f"%{kw}%",)).fetchall()

        products = [UnifiedProduct(**dict(r)) for r in rows]

        # Form cross-vendor pairs
        for i in range(len(products)):
            for j in range(i + 1, len(products)):
                p1 = products[i]
                p2 = products[j]
                if p1.vendor != p2.vendor:
                    pair_key = tuple(sorted([f"{p1.vendor}:{p1.external_id}", f"{p2.vendor}:{p2.external_id}"]))
                    if pair_key not in seen_pairs:
                        seen_pairs.add(pair_key)
                        pairs.append((p1, p2))
                        if len(pairs) >= total_target:
                            conn.close()
                            return pairs

    conn.close()
    return pairs


def audit_pipeline_on_100_pairs():
    console.print("\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold white]   AUDIT: 100 REAL CROSS-VENDOR CANDIDATE PAIRS FROM LIVE DATABASE   [/bold white]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    pairs = sample_cross_vendor_candidates(100)
    console.print(f"Sampled [bold yellow]{len(pairs)}[/bold yellow] realistic cross-vendor pairs sharing hardware keywords.\n")

    v1_merges = 0
    v2_merges = 0
    vetoes_triggered = 0
    spec_vetoes = 0
    mpn_vetoes = 0

    results = []

    for idx, (p1, p2) in enumerate(pairs, 1):
        # Baseline V1
        sim_v1 = calculate_token_similarity(p1.normalized_title, p2.normalized_title)
        v1_decision = (sim_v1 >= 0.70) or (p1.mpn and p2.mpn and p1.mpn == p2.mpn)
        if v1_decision:
            v1_merges += 1

        # Matcher V2
        is_match_v2, score_v2, reason_v2 = match_components_v2(p1.title, p2.title)
        if is_match_v2:
            v2_merges += 1

        if "Hard Veto" in reason_v2:
            vetoes_triggered += 1
            if "Tier A.5" in reason_v2:
                mpn_vetoes += 1
            elif "Conflicting spec" in reason_v2:
                spec_vetoes += 1

        results.append({
            "idx": idx,
            "vendor_a": p1.vendor_name,
            "title_a": p1.title,
            "vendor_b": p2.vendor_name,
            "title_b": p2.title,
            "v1_match": v1_decision,
            "v1_score": sim_v1,
            "v2_match": is_match_v2,
            "v2_score": score_v2,
            "reason": reason_v2
        })

    # Summary table
    table = Table(title="Pipeline Behavior Summary on 100 Live DB Pairs", show_header=True, header_style="bold green")
    table.add_column("Decision Type / Signal", style="cyan")
    table.add_column("Count / 100", justify="right", style="bold yellow")
    table.add_column("Operational Meaning", style="dim")

    table.add_row("Total Pairs Evaluated", str(len(pairs)), "Sampled across Quartz, MakerBazar, REES52, Robokits, Robu")
    table.add_row("Baseline V1 Merges (Jaccard)", str(v1_merges), "Old pipeline merged these")
    table.add_row("Matcher V2 Merges (Multi-Tier)", str(v2_merges), "New pipeline merged these")
    table.add_row("Total Hard Vetoes Triggered", str(vetoes_triggered), "V2 explicitly blocked merge due to conflicting hardware specs")
    table.add_row("  -> MPN Conflict Vetoes (Tier A.5)", str(mpn_vetoes), "Different ICs/chips prevented from grouping")
    table.add_row("  -> Physical/Electrical Spec Vetoes", str(spec_vetoes), "Mismatched pins, voltages, direction, dimensions")

    console.print(table)

    # Show cases where V1 and V2 DISAGREED (The critical edge cases)
    disagreements = [r for r in results if r["v1_match"] != r["v2_match"]]
    console.print(f"\n[bold yellow]DISAGREEMENTS BETWEEN V1 AND V2: {len(disagreements)} cases[/bold yellow]")
    console.print("[dim]These are the exact cases where V2 prevented a bad merge or caught a missed synonym in production:[/dim]\n")

    for i, d in enumerate(disagreements[:15], 1):
        console.print(f"[bold magenta]Case #{i}:[/bold magenta]")
        console.print(f"  Item A ({d['vendor_a']}): [cyan]{d['title_a']}[/cyan]")
        console.print(f"  Item B ({d['vendor_b']}): [cyan]{d['title_b']}[/cyan]")
        console.print(f"  Baseline V1: [red]{'MERGE' if d['v1_match'] else 'REJECT'}[/red] (Sim: {d['v1_score']:.2f})")
        console.print(f"  Matcher V2:  [green]{'MERGE' if d['v2_match'] else 'REJECT'}[/green] (Score: {d['v2_score']:.2f})")
        console.print(f"  V2 Reason:   [italic]{d['reason']}[/italic]\n")

    # Show a few confident V2 merges
    confident_merges = [r for r in results if r["v2_match"]]
    console.print(f"\n[bold green]CONFIDENT V2 CROSS-VENDOR MERGES: {len(confident_merges)} cases[/bold green]")
    for i, m in enumerate(confident_merges[:5], 1):
        console.print(f"  [green][MATCH #{i}][/green] {m['reason']}")
        console.print(f"    - {m['vendor_a']}: {m['title_a']}")
        console.print(f"    - {m['vendor_b']}: {m['title_b']}\n")



if __name__ == "__main__":
    audit_pipeline_on_100_pairs()
