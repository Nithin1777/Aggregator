"""
Prototype and verification script for Round 4 Agent-First MCP Server.
"""
import base64
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import db
from models import UnifiedProduct
from search_service import AggregatorEngine

SERVER_VERSION = "1.4.0"
SCHEMA_VERSION = "1.4.0"

_engine = AggregatorEngine()

def get_db():
    return db.get_db_connection()

def make_response(
    data: Any,
    total_matches: int = 0,
    returned: int = 0,
    truncated: bool = False,
    next_cursor: Optional[str] = None,
    filters_applied: Optional[Dict[str, Any]] = None,
    coverage: Optional[Dict[str, Any]] = None,
    warnings: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "data": data,
        "total_matches": total_matches,
        "returned": returned,
        "truncated": truncated,
        "next_cursor": next_cursor,
        "filters_applied": filters_applied or {},
        "coverage": coverage or {},
        "warnings": warnings or [],
        "server_version": SERVER_VERSION,
        "schema_version": SCHEMA_VERSION,
    }

def make_error(
    error_code: str,
    message: str,
    valid_keys: Optional[List[str]] = None,
    suggestions: Optional[List[Dict[str, Any]]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "valid_keys": valid_keys,
        "suggestions": suggestions or [],
        "details": details or {},
        "server_version": SERVER_VERSION,
    }

def normalize_scalar(key: str, val: Any, v_type: str = "string", unit: Optional[str] = None) -> Any:
    s = str(val).strip()

    # Cells (4S, 4 s, 4s lipo)
    if key == "cells" or (isinstance(s, str) and re.search(r"^\d+\s*s(\s+lipo|\s+li-ion)?$", s, re.I)):
        m = re.search(r"^(\d+)\s*s", s, re.I)
        if m:
            return f"{m.group(1)}s".lower()

    # Torque (2.5kg-cm / 2.5 kgcm)
    if "torque" in key or (isinstance(s, str) and re.search(r"kg[\s-]*cm", s, re.I)):
        m_t = re.search(r"^([\d\.]+)\s*kg[\s-]*cm$", s, re.I)
        if m_t:
            return float(m_t.group(1))

    # Capacity (mAh / Ah)
    if key == "capacity_mah" or unit == "mAh":
        m_ah = re.search(r"^([\d\.]+)\s*ah$", s, re.I)
        if m_ah:
            return int(round(float(m_ah.group(1)) * 1000))
        m_mah = re.search(r"^([\d\.]+)\s*(?:mah)?$", s, re.I)
        if m_mah:
            return int(round(float(m_mah.group(1))))

    # Voltage (V / VDC)
    if key == "voltage" or unit == "V":
        m_v = re.search(r"^([\d\.]+)\s*(?:v(?:dc)?)?$", s, re.I)
        if m_v:
            v_num = m_v.group(1)
            if v_num.endswith(".0"):
                v_num = v_num[:-2]
            return v_num

    # RPM
    if key == "rpm" or unit == "RPM":
        m_rpm = re.search(r"^(\d+)\s*(?:rpm)?$", s, re.I)
        if m_rpm:
            return int(m_rpm.group(1))

    # Current (A / mA)
    if key == "current_a" or unit == "A":
        m_ma = re.search(r"^([\d\.]+)\s*ma$", s, re.I)
        if m_ma:
            return round(float(m_ma.group(1)) / 1000.0, 3)
        m_a = re.search(r"^([\d\.]+)\s*(?:a|amp|amps)?$", s, re.I)
        if m_a:
            return float(m_a.group(1))

    # Channels
    if key == "channels":
        m_ch = re.search(r"^(\d+)\s*(?:ch|channel|channels)?$", s, re.I)
        if m_ch:
            return int(m_ch.group(1))

    # NEMA
    if re.search(r"^nema\s*\d+$", s, re.I):
        return re.sub(r"\s+", "", s).lower()

    if v_type in ("integer", "float"):
        try:
            return int(s) if v_type == "integer" else float(s)
        except ValueError:
            pass

    return s.lower()

def _load_catalog_specs(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("""
        SELECT g.key, g.value_type, sd.unit, g.applicable_count, sd.display_name, sd.enum_values, g.value_min, g.value_max, g.value_counts
        FROM spec_stats_global g
        LEFT JOIN (
            SELECT key, unit, display_name, enum_values FROM spec_definitions GROUP BY key
        ) sd ON g.key = sd.key
        WHERE g.applicable_count >= 50
        ORDER BY g.applicable_count DESC;
    """)
    specs = {}
    for r in cur.fetchall():
        specs[r[0]] = {
            "key": r[0],
            "type": r[1],
            "unit": r[2],
            "count": r[3],
            "display_name": r[4] or r[0].replace("_", " ").title(),
            "enum_values": json.loads(r[5]) if r[5] else None,
            "min": r[6],
            "max": r[7],
            "value_counts": json.loads(r[8]) if r[8] else {}
        }
    return specs

print("Testing _load_catalog_specs...")
c = get_db()
s = _load_catalog_specs(c)
print("Keys loaded:", list(s.keys()))
c.close()
