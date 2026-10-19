"""
Execution of the 8-Step User-Designed Evaluation Benchmark:
1. Gray-zone logging active
2. Draw 150 random items (30 per vendor)
3. Record EVERY merge - inspect with expert domain reasoning
4. Record EVERY high-similarity veto - inspect with expert domain reasoning
5. MPN-anchored cross-vendor non-merge query (same MPN, didn't merge)
6. Skip near-threshold no-merges
7. Add discovered failures to eval set as adversarial_observed
8. Separately draw 100 fresh cross-vendor keyword pairs -> precision measurement
"""
import json
import random
import re
import sqlite3
from typing import Any, Dict, List, Tuple
from rich.console import Console
from rich.table import Table

from config import settings
from models import UnifiedProduct
from normalizer import (
    calculate_token_similarity,
    canonical_base_mpn,
    extract_hardware_specs,
    extract_mpn,
    get_canonical_cluster_key_v2,
    match_components_v2,
    normalize_title,
)
from search_service import AggregatorEngine

console = Console()

def get_db():
    conn = sqlite3.connect(str(settings.db_path))
    conn.row_factory = sqlite3.Row
    return conn

# ---------------------------------------------------------------------------
# STEP 2: Draw 150 random items (30 per vendor)
# ---------------------------------------------------------------------------
def draw_150_items(seed: int = 42) -> List[UnifiedProduct]:
    random.seed(seed)
    conn = get_db()
    vendors = ["quartzcomponents", "makerbazar", "rees52", "robokits", "robu"]
    items_150: List[UnifiedProduct] = []

    for v in vendors:
        rows = conn.execute("""
            SELECT vendor, vendor_name, external_id, title, normalized_title, mpn, sku, price, available, product_url, image_url
            FROM products
            WHERE vendor = ?
            ORDER BY RANDOM()
            LIMIT 30;
        """, (v,)).fetchall()
        items_150.extend([UnifiedProduct(**dict(r)) for r in rows])

    conn.close()
    return items_150


def run_test_suite():
    console.print("\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold white]   USER-DESIGNED 8-STEP BENCHMARK & EXPERT REASONING AUDIT       [/bold white]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    # Step 1: Ensure gray-zone logging is on
    console.print(f"[bold green]Step 1:[/bold green] Gray-zone logging active: [bold]{settings.matcher_audit_logging}[/bold]")

    # Step 2: Draw 150 random items
    items_150 = draw_150_items(seed=42)
    console.print(f"[bold green]Step 2:[/bold green] Sampled [bold yellow]{len(items_150)}[/bold yellow] items (30 from Quartz, 30 MakerBazar, 30 REES52, 30 Robokits, 30 Robu).")

    # Step 3: Run through pipeline and record EVERY merge
    console.print(f"\n[bold green]Step 3: Recording EVERY Merge in the 150-Item Draw[/bold green]")
    engine = AggregatorEngine()
    groups = engine.group_and_compare(items_150)
    merged_groups = [g for g in groups if len(g.offers) > 1]

    console.print(f"Total groups formed: [bold cyan]{len(groups)}[/bold cyan] (Singletons: {len(groups) - len(merged_groups)}, Merged Cards: {len(merged_groups)})\n")

    discovered_failures = []
    
    if not merged_groups:
        console.print("[dim]No merges occurred in this random 150-item draw (items were distinct components).[/dim]")
    else:
        for i, g in enumerate(merged_groups, 1):
            console.print(f"[bold yellow]Merged Group #{i}:[/bold yellow] '{g.canonical_name}' ({g.total_offers} offers across vendors)")
            offers = g.offers
            for off in offers:
                console.print(f"   • [{off.vendor_name}] {off.title} | Rs. {off.price}")

    # Inspect all pairwise comparisons across vendors among the 150 items
    all_pairs: List[Tuple[UnifiedProduct, UnifiedProduct]] = []
    for i in range(len(items_150)):
        for j in range(i + 1, len(items_150)):
            if items_150[i].vendor != items_150[j].vendor:
                all_pairs.append((items_150[i], items_150[j]))

    pairwise_merges = []
    high_sim_vetoes = []
    same_mpn_non_merges = []

    for p1, p2 in all_pairs:
        is_match, score, reason = match_components_v2(p1.title, p2.title, p1.price, p2.price)
        raw_sim = calculate_token_similarity(normalize_title(p1.title), normalize_title(p2.title))

        base_a = canonical_base_mpn(extract_mpn(p1.title))
        base_b = canonical_base_mpn(extract_mpn(p2.title))

        # Check Step 3: Any pairwise merge
        if is_match:
            pairwise_merges.append((p1, p2, score, reason))

        # Check Step 4: High-similarity veto (raw token sim >= 0.35 or shared MPN but vetoed)
        if not is_match and "Hard Veto" in reason and (raw_sim >= 0.35 or (base_a and base_b and base_a == base_b)):
            high_sim_vetoes.append((p1, p2, raw_sim, score, reason))

        # Check Step 5: Same MPN, didn't merge
        if base_a and base_b and base_a == base_b and not is_match:
            same_mpn_non_merges.append((p1, p2, base_a, score, reason))

    console.print(f"\n[bold cyan]Pairwise Merges Found Across 150 Items: {len(pairwise_merges)}[/bold cyan]")
    for idx, (p1, p2, score, reason) in enumerate(pairwise_merges, 1):
        console.print(f"\n[bold magenta]Merge #{idx}:[/bold magenta]")
        console.print(f"  Item A ({p1.vendor_name}): {p1.title} (Rs. {p1.price})")
        console.print(f"  Item B ({p2.vendor_name}): {p2.title} (Rs. {p2.price})")
        console.print(f"  Matcher Reason: {reason} (Score: {score})")

    # Step 4: Record EVERY high-similarity veto
    console.print(f"\n[bold green]Step 4: Recording EVERY High-Similarity Veto ({len(high_sim_vetoes)} found)[/bold green]")
    for idx, (p1, p2, raw_sim, score, reason) in enumerate(high_sim_vetoes, 1):
        console.print(f"\n[bold yellow]High-Sim Veto #{idx}:[/bold yellow]")
        console.print(f"  Item A ({p1.vendor_name}): {p1.title}")
        console.print(f"  Item B ({p2.vendor_name}): {p2.title}")
        console.print(f"  Raw Sim: {raw_sim:.2f} | Reason: {reason}")

    # Step 5: MPN-anchored cross-vendor non-merge query
    console.print(f"\n[bold green]Step 5: MPN-Anchored Cross-Vendor Non-Merge Query ({len(same_mpn_non_merges)} cases)[/bold green]")
    if not same_mpn_non_merges:
        console.print("[dim]0 cases: Every cross-vendor pair with identical canonical MPN was merged.[/dim]")
    else:
        for idx, (p1, p2, mpn, score, reason) in enumerate(same_mpn_non_merges, 1):
            console.print(f"\n[bold red]Same-MPN Non-Merge #{idx} (MPN: {mpn}):[/bold red]")
            console.print(f"  Item A ({p1.vendor_name}): {p1.title}")
            console.print(f"  Item B ({p2.vendor_name}): {p2.title}")
            console.print(f"  Reason not merged: {reason}")

    # Step 8: Draw 100 fresh cross-vendor keyword pairs for precision measurement
    console.print(f"\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold white]   STEP 8: 100 FRESH CROSS-VENDOR KEYWORD PAIRS (PRECISION TEST) [/bold white]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    conn = get_db()
    keywords = [
        "esp32", "nodemcu", "esp8266", "ch340", "cp2102",
        "lm2596", "ams1117", "xl4015", "tp4056",
        "mpu6050", "bme280", "bmp280", "dht11", "dht22", "hc-sr04",
        "sg90", "a4988", "drv8825", "l298n", "oled"
    ]
    random.seed(101) # fresh seed different from 42
    random.shuffle(keywords)

    used_ext_ids = {p.external_id for p in items_150}
    fresh_pairs: List[Tuple[UnifiedProduct, UnifiedProduct]] = []
    seen_pair_keys = set()

    for kw in keywords:
        rows = conn.execute("""
            SELECT vendor, vendor_name, external_id, title, normalized_title, mpn, sku, price, available, product_url, image_url
            FROM products
            WHERE title LIKE ?
            ORDER BY RANDOM()
            LIMIT 20;
        """, (f"%{kw}%",)).fetchall()
        pool = [UnifiedProduct(**dict(r)) for r in rows if r["external_id"] not in used_ext_ids]

        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                p1, p2 = pool[i], pool[j]
                if p1.vendor != p2.vendor:
                    pk = tuple(sorted([f"{p1.vendor}:{p1.external_id}", f"{p2.vendor}:{p2.external_id}"]))
                    if pk not in seen_pair_keys:
                        seen_pair_keys.add(pk)
                        fresh_pairs.append((p1, p2))
                        if len(fresh_pairs) >= 100:
                            break
            if len(fresh_pairs) >= 100:
                break
        if len(fresh_pairs) >= 100:
            break

    conn.close()
    console.print(f"Sampled [bold yellow]{len(fresh_pairs)}[/bold yellow] fresh cross-vendor candidate pairs.")

    step8_merges = []
    step8_rejections = []

    for p1, p2 in fresh_pairs:
        is_match, score, reason = match_components_v2(p1.title, p2.title, p1.price, p2.price)
        if is_match:
            step8_merges.append((p1, p2, score, reason))
        else:
            step8_rejections.append((p1, p2, score, reason))

    console.print(f"Matcher V2 Decisions on 100 Fresh Keyword Pairs:")
    console.print(f"  • Merged:   [bold green]{len(step8_merges)}[/bold green]")
    console.print(f"  • Rejected: [bold cyan]{len(step8_rejections)}[/bold cyan]")

    return {
        "items_150_count": len(items_150),
        "pairwise_merges": pairwise_merges,
        "high_sim_vetoes": high_sim_vetoes,
        "same_mpn_non_merges": same_mpn_non_merges,
        "step8_merges": step8_merges,
        "step8_rejections": step8_rejections
    }

if __name__ == "__main__":
    run_test_suite()
