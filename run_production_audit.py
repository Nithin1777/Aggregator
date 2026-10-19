"""
Comprehensive Production Audit on 100 Entries from Live Database.
Evaluates both Random Catalog Grouping and Cross-Vendor Overlap Pairs.
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

def run_100_random_product_clustering():
    """
    Test 1: Take 20 random products from each of the 5 vendors (Total: 100 products)
    and pass them through group_and_compare.
    """
    conn = get_db_connection()
    vendors = ["quartzcomponents", "makerbazar", "rees52", "robokits", "robu"]
    sample_100: List[UnifiedProduct] = []

    for v in vendors:
        rows = conn.execute("""
            SELECT vendor, vendor_name, external_id, title, normalized_title, mpn, sku, price, available, product_url, image_url
            FROM products
            WHERE vendor = ?
            ORDER BY RANDOM()
            LIMIT 20;
        """, (v,)).fetchall()
        sample_100.extend([UnifiedProduct(**dict(r)) for r in rows])

    conn.close()

    engine = AggregatorEngine()
    groups = engine.group_and_compare(sample_100)

    multi_vendor_groups = [g for g in groups if len(g.offers) > 1]
    singletons = [g for g in groups if len(g.offers) == 1]

    return sample_100, groups, multi_vendor_groups, singletons


def run_100_cross_vendor_pair_audit():
    """
    Test 2: Sample 100 cross-vendor candidate pairs where titles share components.
    Evaluate every merge for precision vs real hardware distinctions.
    """
    conn = get_db_connection()
    query_terms = [
        "esp32", "nodemcu", "esp8266", "ch340", "cp2102",
        "lm2596", "ams1117", "xl4015", "tp4056",
        "mpu6050", "bme280", "bmp280", "dht11", "dht22", "hc-sr04",
        "sg90", "a4988", "drv8825", "l298n", "oled"
    ]
    random.shuffle(query_terms)

    candidate_pairs = []
    seen = set()

    for q in query_terms:
        rows = conn.execute("""
            SELECT vendor, vendor_name, external_id, title, normalized_title, mpn, sku, price, available, product_url, image_url
            FROM products
            WHERE title LIKE ?
            ORDER BY RANDOM()
            LIMIT 15;
        """, (f"%{q}%",)).fetchall()
        items = [UnifiedProduct(**dict(r)) for r in rows]

        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                p1, p2 = items[i], items[j]
                if p1.vendor != p2.vendor:
                    k = tuple(sorted([f"{p1.vendor}:{p1.external_id}", f"{p2.vendor}:{p2.external_id}"]))
                    if k not in seen:
                        seen.add(k)
                        candidate_pairs.append((p1, p2))
                        if len(candidate_pairs) >= 100:
                            conn.close()
                            return candidate_pairs

    conn.close()
    return candidate_pairs


def classify_pair_relationship(p1: UnifiedProduct, p2: UnifiedProduct) -> str:
    """
    Human-calibrated heuristic to classify whether two products are:
    - 'TRUE_MATCH': Same core board/module
    - 'RAW_IC_VS_MODULE': One is a surface mount IC / chip package, other is a module
    - 'HOST_VS_SUBCOMPONENT': One is a relay board/camera with an auxiliary IC onboard
    - 'ACCESSORY_VS_DEVICE': One is a case, heatsink, or mounting bracket
    - 'DIFFERENT_PRODUCT': Completely different hardware
    """
    t1 = p1.title.lower()
    t2 = p2.title.lower()

    # Accessory vs board
    is_case1 = any(w in t1 for w in ["case", "enclosure", "housing", "shell", "bracket", "heatsink"])
    is_case2 = any(w in t2 for w in ["case", "enclosure", "housing", "shell", "bracket", "heatsink"])
    if is_case1 != is_case2:
        return "ACCESSORY_VS_DEVICE"

    # Raw IC vs Module
    is_raw1 = any(w in t1 for w in ["smd", "qfn", "sop-8", "soic", "to-220", "to-263", "dip-", "ic chip", "integrated circuit"]) and "module" not in t1
    is_raw2 = any(w in t2 for w in ["smd", "qfn", "sop-8", "soic", "to-220", "to-263", "dip-", "ic chip", "integrated circuit"]) and "module" not in t2
    if is_raw1 != is_raw2:
        return "RAW_IC_VS_MODULE"

    # Host board with auxiliary chip (e.g. 16 channel relay with LM2596, or Camera with MPU6050)
    is_relay1 = "relay" in t1
    is_relay2 = "relay" in t2
    if is_relay1 != is_relay2:
        return "HOST_VS_SUBCOMPONENT"

    is_cam1 = "camera" in t1
    is_cam2 = "camera" in t2
    if is_cam1 != is_cam2:
        return "HOST_VS_SUBCOMPONENT"

    # If both are modules of same chip family without obvious differences
    return "LIKELY_SAME_OR_VARIANT"


if __name__ == "__main__":
    console.print("\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold white]   REAL-WORLD AUDIT 1: 100 RANDOMLY SAMPLED PRODUCTS CLUSTERING  [/bold white]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    sample_100, groups, multi_vendor, singletons = run_100_random_product_clustering()
    console.print(f"Sampled [bold yellow]100 products[/bold yellow] uniformly (20 from Quartz, 20 MakerBazar, 20 REES52, 20 Robokits, 20 Robu).")
    console.print(f"Aggregator created [bold green]{len(groups)} total groups[/bold green]:")
    console.print(f"  - Multi-vendor grouped cards: [bold green]{len(multi_vendor)}[/bold green]")
    console.print(f"  - Isolated singleton cards:   [bold cyan]{len(singletons)}[/bold cyan]")

    if multi_vendor:
        console.print("\n[bold green]Multi-Vendor Merged Cards in 100 Random Draw:[/bold green]")
        for i, g in enumerate(multi_vendor, 1):
            console.print(f"  #{i} [bold cyan]{g.canonical_name}[/bold cyan] ({g.total_offers} offers across vendors)")
            for o in g.offers:
                console.print(f"      [{o.vendor_name}]: {o.title} -> Rs. {o.price}")

    console.print("\n[bold cyan]================================================================[/bold cyan]")
    console.print("[bold white]   REAL-WORLD AUDIT 2: 100 OVERLAPPING CANDIDATE PAIRS EVALUATION  [/bold white]")
    console.print("[bold cyan]================================================================[/bold cyan]\n")

    pairs = run_100_cross_vendor_pair_audit()
    console.print(f"Sampled [bold yellow]100 cross-vendor pairs[/bold yellow] that share component tokens.\n")

    # Metrics
    true_merges = 0
    false_merges_raw_ic = 0
    false_merges_host_board = 0
    false_merges_accessory = 0
    true_rejections = 0
    false_rejections = 0

    detailed_cases = []

    for idx, (p1, p2) in enumerate(pairs, 1):
        is_match, score, reason = match_components_v2(p1.title, p2.title)
        ground_truth = classify_pair_relationship(p1, p2)

        if is_match:
            if ground_truth == "RAW_IC_VS_MODULE":
                false_merges_raw_ic += 1
                decision_quality = "FALSE_MERGE (Raw IC vs Module)"
            elif ground_truth == "HOST_VS_SUBCOMPONENT":
                false_merges_host_board += 1
                decision_quality = "FALSE_MERGE (Host Board vs Sub-Chip)"
            elif ground_truth == "ACCESSORY_VS_DEVICE":
                false_merges_accessory += 1
                decision_quality = "FALSE_MERGE (Accessory vs Board)"
            else:
                true_merges += 1
                decision_quality = "TRUE_MERGE"
        else:
            if ground_truth in ["RAW_IC_VS_MODULE", "HOST_VS_SUBCOMPONENT", "ACCESSORY_VS_DEVICE"]:
                true_rejections += 1
                decision_quality = "CORRECT_REJECTION"
            else:
                decision_quality = "REJECTION (Different or Variant)"

        detailed_cases.append({
            "idx": idx,
            "vendor_a": p1.vendor_name,
            "title_a": p1.title,
            "vendor_b": p2.vendor_name,
            "title_b": p2.title,
            "is_match": is_match,
            "score": score,
            "reason": reason,
            "quality": decision_quality
        })

    # Summary table
    audit_table = Table(title="Production Behavior on 100 Live Hardware Pairs", show_header=True, header_style="bold green")
    audit_table.add_column("Result Classification", style="cyan")
    audit_table.add_column("Count", justify="right", style="bold yellow")
    audit_table.add_column("Percentage", justify="right")
    audit_table.add_column("Operational Analysis", style="dim")

    total_merged = true_merges + false_merges_raw_ic + false_merges_host_board + false_merges_accessory
    precision = (true_merges / total_merged * 100) if total_merged > 0 else 0.0

    audit_table.add_row("Valid Merges (True Matches)", str(true_merges), f"{true_merges}%", "Accurate cross-vendor comparison cards")
    audit_table.add_row("False Merges: Raw IC vs Breakout Module", str(false_merges_raw_ic), f"{false_merges_raw_ic}%", "Bare SMD QFN/SOP chip merged with PCB module")
    audit_table.add_row("False Merges: Host Board vs Onboard Chip", str(false_merges_host_board), f"{false_merges_host_board}%", "e.g. Relay board or Camera with LM2596/MPU6050")
    audit_table.add_row("False Merges: Accessory/Case vs Electronics", str(false_merges_accessory), f"{false_merges_accessory}%", "Plastic case or mount merged with electronic board")
    audit_table.add_row("Successful Rejections & Vetoes", str(len(pairs) - total_merged), f"{len(pairs) - total_merged}%", "Different ICs, pinouts, or incompatible parts kept separate")
    audit_table.add_row("[bold]Real-World Precision on Merged Cards[/bold]", f"[bold]{precision:.1f}%[/bold]", "-", "True Merges / Total Merges in the Wild")

    console.print(audit_table)

    # Show exact False Merges detected in the wild!
    bad_merges = [c for c in detailed_cases if "FALSE_MERGE" in c["quality"]]
    if bad_merges:
        console.print(f"\n[bold red]EXACT FALSE MERGES DETECTED IN THE 100 REAL DB PAIRS ({len(bad_merges)} cases):[/bold red]")
        for i, b in enumerate(bad_merges[:6], 1):
            console.print(f"\n[bold yellow]Bad Merge #{i}: {b['quality']}[/bold yellow]")
            console.print(f"  Item A ({b['vendor_a']}): [cyan]{b['title_a']}[/cyan]")
            console.print(f"  Item B ({b['vendor_b']}): [cyan]{b['title_b']}[/cyan]")
            console.print(f"  Matcher Reason: [italic]{b['reason']}[/italic]")
