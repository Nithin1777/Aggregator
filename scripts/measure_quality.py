"""
Quality Metrics and Hand-Labeling Sampler for MCP Pilot Report.
Extracts:
1. Catalog classification breakdown
2. Fill rate per attribute and advertisement status
3. Top 10 unmatched enum voltage values
4. 40 Motor + 40 Battery sample rows for user precision labeling
5. 30 Unclassified LIKE sample rows for recall analysis
6. 20 current_a sample rows for user precision labeling
"""
import json
from pathlib import Path
import random
import sqlite3
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db

def run_metrics():
    conn = db.get_db_connection()
    cur = conn.cursor()

    print("=" * 70)
    print("1. CLASSIFICATION BREAKDOWN")
    print("=" * 70)
    cur.execute("SELECT COUNT(1) FROM products;")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(1) FROM products WHERE spec_category = 'motor';")
    m_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(1) FROM products WHERE spec_category = 'battery';")
    b_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(1) FROM products WHERE spec_category IS NULL;")
    null_cnt = cur.fetchone()[0]

    print(f"Total catalog products: {total}")
    print(f"  Motor:   {m_cnt:>6} ({m_cnt/total:.2%})")
    print(f"  Battery: {b_cnt:>6} ({b_cnt/total:.2%})")
    print(f"  NULL:    {null_cnt:>6} ({null_cnt/total:.2%})")

    print("\n" + "=" * 70)
    print("2. FILL RATE TABLE")
    print("=" * 70)
    cur.execute("""
        SELECT category, key, applicable_count, filled_count, value_min, value_max
        FROM spec_stats
        ORDER BY category, key;
    """)
    stats_rows = cur.fetchall()
    print(f"{'Category.Attribute':<25} | {'Filled':<10} | {'Applicable':<10} | {'Fill Rate':<10} | {'Advertised (>=40%)'}")
    print("-" * 75)
    for cat, key, app, fld, v_min, v_max in stats_rows:
        fr = fld / app if app else 0.0
        adv = "YES" if fr >= 0.4 else "NO"
        print(f"{cat + '.' + key:<25} | {fld:<10} | {app:<10} | {fr:<10.1%} | {adv}")

    print("\n" + "=" * 70)
    print("3. TOP 10 UNMATCHED_ENUM VOLTAGE VALUES")
    print("=" * 70)
    cur.execute("""
        SELECT raw, COUNT(1) as cnt
        FROM spec_extraction_warnings
        WHERE key = 'voltage' AND reason = 'unmatched_enum'
        GROUP BY raw
        ORDER BY cnt DESC
        LIMIT 10;
    """)
    warn_rows = cur.fetchall()
    for raw, cnt in warn_rows:
        print(f"  {raw}V: {cnt} occurrences")

    # 4. Precision samples (40 motor + 40 battery)
    print("\n" + "=" * 70)
    print("4. PRECISION SAMPLE (40 Motor + 40 Battery)")
    print("=" * 70)
    random.seed(42)  # Deterministic sampling

    cur.execute("SELECT id, title FROM products WHERE spec_category = 'motor';")
    all_motors = cur.fetchall()
    motor_sample = random.sample(all_motors, min(40, len(all_motors)))

    cur.execute("SELECT id, title FROM products WHERE spec_category = 'battery';")
    all_batteries = cur.fetchall()
    battery_sample = random.sample(all_batteries, min(40, len(all_batteries)))

    def get_specs_for_prod(pid):
        cur.execute("SELECT key, value FROM product_specs WHERE product_id = ?;", (pid,))
        return {r[0]: r[1] for r in cur.fetchall()}

    motor_sample_data = []
    for pid, title in motor_sample:
        specs = get_specs_for_prod(pid)
        motor_sample_data.append((pid, title, specs))

    battery_sample_data = []
    for pid, title in battery_sample:
        specs = get_specs_for_prod(pid)
        battery_sample_data.append((pid, title, specs))

    # 5. Recall Sample (Top 30 unclassified titles matching LIKE)
    print("\n" + "=" * 70)
    print("5. RECALL SAMPLE (Top 30 Unclassified LIKE Matches)")
    print("=" * 70)
    cur.execute("""
        SELECT id, title FROM products
        WHERE spec_category IS NULL
          AND (
            title LIKE '%motor%' OR title LIKE '%motors%' OR
            title LIKE '%batter%' OR title LIKE '%lipo%' OR
            title LIKE '%li-ion%'
          )
        LIMIT 30;
    """)
    recall_sample = cur.fetchall()

    # 6. current_a Sample (20 rows)
    print("\n" + "=" * 70)
    print("6. CURRENT_A PRECISION SAMPLE (20 Rows)")
    print("=" * 70)
    cur.execute("""
        SELECT p.id, p.title, ps.value, ps.evidence
        FROM product_specs ps
        JOIN products p ON ps.product_id = p.id
        WHERE ps.key = 'current_a'
        LIMIT 20;
    """)
    current_sample = cur.fetchall()

    conn.close()
    return {
        "total": total,
        "motor_count": m_cnt,
        "battery_count": b_cnt,
        "null_count": null_cnt,
        "fill_rates": stats_rows,
        "voltage_warnings": warn_rows,
        "motor_sample": motor_sample_data,
        "battery_sample": battery_sample_data,
        "recall_sample": recall_sample,
        "current_sample": current_sample
    }

if __name__ == "__main__":
    run_metrics()
