"""
Extracts blind pilot labeling tasks into data/pilot_labeling_tasks.jsonl
Adheres strictly to the requested schema and blinding rules.
"""
import json
import os
from pathlib import Path
import random
import sqlite3
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db

def export_tasks():
    conn = db.get_db_connection()
    cur = conn.cursor()
    random.seed(42)

    output_path = BASE_DIR / "data" / "pilot_labeling_tasks.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_a = []
    rows_b = []
    rows_c = []
    rows_d = []

    # -------------------------------------------------------------
    # Task A (40 rows) — motor classification
    # -------------------------------------------------------------
    cur.execute("SELECT id, title, price, vendor FROM products WHERE spec_category = 'motor';")
    all_m = cur.fetchall()
    sample_m = random.sample(all_m, 40)

    for i, (pid, title, price, vendor) in enumerate(sample_m, 1):
        cur.execute("SELECT key, value, value_num FROM product_specs WHERE product_id = ?;", (pid,))
        specs = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        voltage = specs["voltage"][0] if "voltage" in specs else None
        rpm = int(round(specs["rpm"][1])) if "rpm" in specs and specs["rpm"][1] is not None else None
        current_a = float(specs["current_a"][1]) if "current_a" in specs and specs["current_a"][1] is not None else None

        rows_a.append({
            "task": "classify_motor",
            "row_id": f"A_{i:03d}",
            "title": str(title) if title is not None else "",
            "extracted_specs": {
                "voltage": voltage,
                "rpm": rpm,
                "current_a": current_a
            },
            "price": float(price) if price is not None else 0.0,
            "vendor": str(vendor) if vendor is not None else ""
        })

    # -------------------------------------------------------------
    # Task B (40 rows) — battery classification
    # -------------------------------------------------------------
    cur.execute("SELECT id, title, price, vendor FROM products WHERE spec_category = 'battery';")
    all_b = cur.fetchall()
    sample_b = random.sample(all_b, 40)

    for i, (pid, title, price, vendor) in enumerate(sample_b, 1):
        cur.execute("SELECT key, value, value_num FROM product_specs WHERE product_id = ?;", (pid,))
        specs = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cells = specs["cells"][0] if "cells" in specs else None
        capacity_mah = int(round(specs["capacity_mah"][1])) if "capacity_mah" in specs and specs["capacity_mah"][1] is not None else None
        chemistry = specs["chemistry"][0] if "chemistry" in specs else None

        rows_b.append({
            "task": "classify_battery",
            "row_id": f"B_{i:03d}",
            "title": str(title) if title is not None else "",
            "extracted_specs": {
                "cells": cells,
                "capacity_mah": capacity_mah,
                "chemistry": chemistry
            },
            "price": float(price) if price is not None else 0.0,
            "vendor": str(vendor) if vendor is not None else ""
        })

    # -------------------------------------------------------------
    # Task C (30 rows) — recall candidates
    # -------------------------------------------------------------
    cur.execute("""
        SELECT id, title, price, vendor FROM products
        WHERE spec_category IS NULL
          AND (
            title LIKE '%motor%' OR title LIKE '%motors%' OR
            title LIKE '%batter%' OR title LIKE '%lipo%' OR
            title LIKE '%li-ion%'
          )
        LIMIT 30;
    """)
    for i, (pid, title, price, vendor) in enumerate(cur.fetchall(), 1):
        rows_c.append({
            "task": "check_recall",
            "row_id": f"C_{i:03d}",
            "title": str(title) if title is not None else "",
            "current_category": None,
            "price": float(price) if price is not None else 0.0,
            "vendor": str(vendor) if vendor is not None else ""
        })

    # -------------------------------------------------------------
    # Task D (20 rows) — current_a extraction check
    # -------------------------------------------------------------
    cur.execute("""
        SELECT p.id, p.title, p.price, p.vendor, ps.value_num, ps.evidence
        FROM product_specs ps
        JOIN products p ON ps.product_id = p.id
        WHERE ps.key = 'current_a'
        LIMIT 20;
    """)
    for i, (pid, title, price, vendor, val_num, ev) in enumerate(cur.fetchall(), 1):
        rows_d.append({
            "task": "check_current",
            "row_id": f"D_{i:03d}",
            "title": str(title) if title is not None else "",
            "extracted_current_a": float(val_num) if val_num is not None else 0.0,
            "evidence_snippet": str(ev) if ev else None,
            "price": float(price) if price is not None else 0.0,
            "vendor": str(vendor) if vendor is not None else ""
        })

    conn.close()

    # Write all tasks sequentially to JSONL
    all_rows = rows_a + rows_b + rows_c + rows_d
    with open(output_path, "w", encoding="utf-8") as f:
        for row in all_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    file_size = os.path.getsize(output_path)

    # Print summary output as required
    print("=" * 60)
    print("EXTRACTION COMPLETE")
    print("=" * 60)
    print(f"File Path: {output_path}")
    print(f"Total Rows: {len(all_rows)}")
    print(f"  Task A (classify_motor):   {len(rows_a)} rows")
    print(f"  Task B (classify_battery): {len(rows_b)} rows")
    print(f"  Task C (check_recall):     {len(rows_c)} rows")
    print(f"  Task D (check_current):    {len(rows_d)} rows")
    print(f"File Size: {file_size} bytes ({file_size / 1024:.2f} KB)")
    print("\n" + "=" * 60)
    print("FIRST LINE OF EACH TASK:")
    print("=" * 60)
    print("TASK A:")
    print(json.dumps(rows_a[0], ensure_ascii=False))
    print("\nTASK B:")
    print(json.dumps(rows_b[0], ensure_ascii=False))
    print("\nTASK C:")
    print(json.dumps(rows_c[0], ensure_ascii=False))
    print("\nTASK D:")
    print(json.dumps(rows_d[0], ensure_ascii=False))

if __name__ == "__main__":
    export_tasks()
