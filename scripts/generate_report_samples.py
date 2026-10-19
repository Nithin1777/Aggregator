"""
Generates markdown tables for:
- 40 motor sample rows for human precision labeling
- 40 battery sample rows for human precision labeling
- 30 unclassified recall sample rows
- 20 current_a sample rows for human precision labeling
"""
from pathlib import Path
import random
import sqlite3
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db

def generate():
    conn = db.get_db_connection()
    cur = conn.cursor()
    random.seed(42)

    def get_specs(pid):
        cur.execute("SELECT key, value FROM product_specs WHERE product_id = ?;", (pid,))
        return dict(cur.fetchall())

    # 1. Motor 40
    cur.execute("SELECT id, title FROM products WHERE spec_category = 'motor';")
    motors = random.sample(cur.fetchall(), 40)
    motor_md = ["| # | ID | Product Title | Extracted Specs | Human Label [Correct / Incorrect] |", "| :--- | :--- | :--- | :--- | :--- |"]
    for i, (pid, title) in enumerate(motors, 1):
        sp = get_specs(pid)
        sp_str = ", ".join(f"`{k}: {v}`" for k, v in sp.items()) if sp else "*(none)*"
        clean_title = title.replace("|", "-").strip()
        motor_md.append(f"| {i} | {pid} | {clean_title} | {sp_str} | [Pending Human Review] |")

    # 2. Battery 40
    cur.execute("SELECT id, title FROM products WHERE spec_category = 'battery';")
    batteries = random.sample(cur.fetchall(), 40)
    battery_md = ["| # | ID | Product Title | Extracted Specs | Human Label [Correct / Incorrect] |", "| :--- | :--- | :--- | :--- | :--- |"]
    for i, (pid, title) in enumerate(batteries, 1):
        sp = get_specs(pid)
        sp_str = ", ".join(f"`{k}: {v}`" for k, v in sp.items()) if sp else "*(none)*"
        clean_title = title.replace("|", "-").strip()
        battery_md.append(f"| {i} | {pid} | {clean_title} | {sp_str} | [Pending Human Review] |")

    # 3. Recall 30
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
    recall_rows = cur.fetchall()
    recall_md = ["| # | ID | Product Title | Real Miss? | Agent Analysis / Reason Unclassified |", "| :--- | :--- | :--- | :--- | :--- |"]
    for i, (pid, title) in enumerate(recall_rows, 1):
        clean_title = title.replace("|", "-").strip()
        t_low = clean_title.lower()
        # Analyze why unclassified
        # Check if keyword had word boundaries or was in compound word or category tie
        is_miss = "No"
        reason = "Compound token without word boundary"
        if "motor" in t_low or "motors" in t_low:
            if "servo" in t_low or "driver" in t_low or "wheel" in t_low or "propeller" in t_low:
                reason = "Accessory / driver / wheel rather than bare motor"
            elif any(w in t_low for w in ["battery", "lipo"]):
                reason = "Tie between motor and battery keywords (classified as NULL by design)"
            else:
                is_miss = "Yes"
                reason = "Real motor miss (e.g. boundary or non-standard tokenization)"
        elif "batter" in t_low or "lipo" in t_low or "li-ion" in t_low:
            if "holder" in t_low or "case" in t_low or "charger" in t_low or "bms" in t_low:
                reason = "Battery accessory / charger / holder"
            else:
                is_miss = "Yes"
                reason = "Real battery miss"
        recall_md.append(f"| {i} | {pid} | {clean_title} | {is_miss} | {reason} |")

    # 4. current_a 20
    cur.execute("""
        SELECT p.id, p.title, ps.value, ps.evidence
        FROM product_specs ps
        JOIN products p ON ps.product_id = p.id
        WHERE ps.key = 'current_a'
        LIMIT 20;
    """)
    current_rows = cur.fetchall()
    current_md = ["| # | ID | Product Title | Extracted Current (A) | Evidence | Human Label [Correct / False Positive] |", "| :--- | :--- | :--- | :--- | :--- | :--- |"]
    for i, (pid, title, val, ev) in enumerate(current_rows, 1):
        clean_title = title.replace("|", "-").strip()
        current_md.append(f"| {i} | {pid} | {clean_title} | `{val} A` | `{ev}` | [Pending Human Review] |")

    conn.close()

    out_file = BASE_DIR / "data" / "report_tables.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write("### MOTOR PRECISION SAMPLE (40 Rows)\n\n" + "\n".join(motor_md) + "\n\n")
        f.write("### BATTERY PRECISION SAMPLE (40 Rows)\n\n" + "\n".join(battery_md) + "\n\n")
        f.write("### RECALL SAMPLE (Top 30 Unclassified LIKE Titles)\n\n" + "\n".join(recall_md) + "\n\n")
        f.write("### CURRENT_A PRECISION SAMPLE (20 Rows)\n\n" + "\n".join(current_md) + "\n\n")

    print(f"Report tables written to {out_file}")

if __name__ == "__main__":
    generate()
