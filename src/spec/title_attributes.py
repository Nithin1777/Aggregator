"""
Title-derived attributes for vendors whose descriptions cannot be fetched.

Robu product pages are behind Cloudflare, but the local catalog already has
85k titles. Many component titles encode the value directly ("10K 1/4W
resistor", "100uF 25V", "NEMA17 2A"). This module extracts those values into the
same free-form `product_attributes` layer as description tables.

It is deliberately conservative: kit/assortment titles are skipped (they contain
ranges, not one part), only single values are stored, and confidence is lower
than a vendor specification table. These attributes are for fit checks; they do
not enter the taxonomy filter layer.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_KIT_RE = re.compile(
    r"\b(kit|assortment|set\s+of|pack\s+of|\d+\s*(?:pcs|pieces)|values?)\b",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(r"\d\s*[a-zA-Zμµ%]{0,4}\s*[-–]\s*\d")

_RESISTANCE_RE = re.compile(
    r"(?<![\d./-])(\d+(?:\.\d+)?)\s*(k|m|r)?\s*(?:ohms?|Ω)\b"
    r"|(?<![\d./-])(\d+(?:\.\d+)?)\s*(k|m|r)\s*(?=(?:resistor|film|metal|smd|$))",
    re.IGNORECASE,
)
_CAPACITANCE_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(pf|nf|uf|µf|mf)\b", re.IGNORECASE
)
_INDUCTANCE_RE = re.compile(
    r"(?<![a-z0-9.])(\d+(?:\.\d+)?)\s*(uh|µh|mh|h)\b", re.IGNORECASE
)
_VOLTAGE_RE = re.compile(
    r"(?<![\w.-])(\d+(?:\.\d+)?)\s*(mv|v|vdc|vac)\b", re.IGNORECASE
)
_CURRENT_RE = re.compile(
    r"(?<![\w.-])(\d+(?:\.\d+)?)\s*(ma|ua|a|amp|amps)\b", re.IGNORECASE
)
_POWER_RE = re.compile(
    r"(?<![\w.-])(\d+(?:\.\d+)?|\d+/\d+)\s*(?:w|watt|watts)\b", re.IGNORECASE
)
_TOLERANCE_RE = re.compile(
    r"(?:±|\+/-)\s*(\d+(?:\.\d+)?)\s*%|(?<![\w.])(\d+(?:\.\d+)?)\s*%"
)
_CAPACITY_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*mah\b", re.IGNORECASE)
_RPM_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*rpm\b", re.IGNORECASE)
_PINS_RE = re.compile(r"(?<![\w.])(\d{1,3})\s*(?:pin|pins)\b", re.IGNORECASE)
_PACKAGE_RE = re.compile(
    r"\b(0201|0402|0603|0805|1206|1210|2010|2512|"
    r"SOT-?23|SOT-?223|TO-?220|TO-?247|TO-?92|"
    r"DIP-?\d{1,3}|SOIC-?\d{1,3}|SOP-?\d{1,3}|"
    r"QFN-?\d{1,3}|TQFP-?\d{1,3}|LQFP-?\d{1,3}|BGA-?\d{1,3}|SMD)\b",
    re.IGNORECASE,
)


def _number(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _add(
    out: Dict[str, Dict[str, Any]],
    key: str,
    label: str,
    value: str,
    value_num: Optional[float],
    unit: Optional[str],
    confidence: float = 0.6,
) -> None:
    if key in out or value_num is None and not value:
        return
    out[key] = {
        "key": key,
        "label": label,
        "value": str(value).strip(),
        "value_num": value_num,
        "unit": unit,
        "source": "title_heuristic",
        "confidence": confidence,
    }


def extract_title_attributes(
    title: str,
    category: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Extracts single-value electrical attributes from one product title."""
    text = (title or "").strip()
    if not text or _KIT_RE.search(text):
        return []

    out: Dict[str, Dict[str, Any]] = {}
    category = (category or "").lower()

    if category in ("resistors", "potentiometers", "") and not _RANGE_RE.search(text):
        match = _RESISTANCE_RE.search(text)
        if match:
            raw = _number(match.group(1) or match.group(3))
            multiplier = (match.group(2) or match.group(4) or "r").lower()
            if raw is not None:
                factor = {"k": 1_000.0, "m": 1_000_000.0, "r": 1.0}.get(multiplier, 1.0)
                ohms = raw * factor
                _add(out, "resistance", "Resistance", match.group(0), ohms, "ohm")

    cap = _CAPACITANCE_RE.search(text)
    if cap:
        raw = _number(cap.group(1))
        if raw is not None:
            unit = cap.group(2).lower().replace("µ", "u").replace("μ", "u")
            factor = {"pf": 1.0, "nf": 1_000.0, "uf": 1_000_000.0, "mf": 1_000_000_000.0}[unit]
            pf = raw * factor
            _add(out, "capacitance", "Capacitance", cap.group(0), pf, "pf")

    ind = _INDUCTANCE_RE.search(text)
    if ind:
        raw = _number(ind.group(1))
        if raw is not None:
            unit = ind.group(2).lower().replace("µ", "u").replace("μ", "u")
            factor = {"uh": 1.0, "mh": 1_000.0, "h": 1_000_000.0}[unit]
            uh = raw * factor
            _add(out, "inductance", "Inductance", f"{uh:g} uH", uh, "uh")

    volt = _VOLTAGE_RE.search(text)
    if volt and not _RANGE_RE.search(text[max(0, volt.start() - 2): volt.end() + 2]):
        raw = _number(volt.group(1))
        unit = (volt.group(2) or "v").lower()
        if raw is not None:
            value = raw / 1000.0 if unit == "mv" else raw
            _add(out, "voltage", "Voltage", f"{value:g} V", value, "v")

    amp = _CURRENT_RE.search(text)
    if amp:
        raw = _number(amp.group(1))
        unit = (amp.group(2) or "a").lower()
        if raw is not None:
            if unit == "ma":
                value = raw / 1000.0
            elif unit == "ua":
                value = raw / 1_000_000.0
            else:
                value = raw
            _add(out, "current", "Current", f"{value:g} A", value, "a")

    power = _POWER_RE.search(text)
    if power:
        raw = power.group(1)
        value = _number(raw)
        if value is None and "/" in raw:
            numerator, denominator = raw.split("/", 1)
            try:
                value = float(numerator) / float(denominator)
            except (TypeError, ValueError, ZeroDivisionError):
                value = None
        if value is not None:
            _add(out, "power", "Power", f"{value:g} W", value, "w")

    tol = _TOLERANCE_RE.search(text)
    if tol:
        raw = _number(tol.group(1) or tol.group(2))
        if raw is not None:
            _add(out, "tolerance", "Tolerance", f"{raw:g} %", raw, "%")

    capacity = _CAPACITY_RE.search(text)
    if capacity:
        raw = _number(capacity.group(1))
        if raw is not None:
            _add(out, "capacity_mah", "Capacity", f"{raw:g} mAh", raw, "mah")

    rpm = _RPM_RE.search(text)
    if rpm:
        raw = _number(rpm.group(1))
        if raw is not None:
            _add(out, "rpm", "Speed", f"{raw:g} RPM", raw, "rpm")

    pins = _PINS_RE.search(text)
    if pins:
        raw = _number(pins.group(1))
        if raw is not None:
            _add(out, "pins", "Pins", f"{int(raw)}", raw, None)

    package = _PACKAGE_RE.search(text)
    if package:
        _add(out, "package", "Package", package.group(1).upper(), None, None)

    return list(out.values())
