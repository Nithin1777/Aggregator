"""
Vendor-description attribute extraction.

TAXONOMY SPECS VS VENDOR ATTRIBUTES
-----------------------------------
`product_specs` is the small, curated, filterable taxonomy layer (motor rpm,
battery chemistry, ...). Vendor description tables contain far more fields than
the taxonomy defines yet: tolerance, package, dielectric, interface, pin count,
dimensions, ...

This module turns those tables/lists into normalized `product_attributes` rows so
agents can answer fit questions immediately, without waiting for every field to
become a taxonomy filter. It deliberately does not write into `product_specs`:
mixing free-form keys into the filter layer would make `filters=` unstable.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup

# Header cells that identify a two-column specification table, not data.
_HEADER_KEYS = {
    "specification", "specifications", "spec", "parameter", "parameters",
    "attribute", "attributes", "feature", "features", "property", "properties",
    "details", "detail", "technical details", "description",
}
_HEADER_VALUES = {
    "value", "values", "details", "detail", "specification", "specifications",
    "description", "remarks", "comment", "comments",
}

_SKIP_KEYS = {
    "description", "features", "feature", "applications", "application",
    "package includes", "package contents", "package content",
    "note", "notes", "warranty", "shipping", "price", "quantity", "moq",
    "country of origin", "sku", "specification", "specifications",
    "parameter", "parameters", "value", "values", "details", "detail",
    "product description", "overview", "introduction", "additional information",
    "also check these products", "related products", "you may also like",
}

_KEY_ALIASES = {
    "operating voltage": "voltage",
    "input voltage": "voltage",
    "supply voltage": "voltage",
    "rated voltage": "voltage",
    "output voltage": "voltage",
    "voltage range": "voltage",
    "operating current": "current",
    "input current": "current",
    "rated current": "current",
    "output current": "current",
    "power rating": "power",
    "rated power": "power",
    "output power": "power",
    "product dimensions": "dimensions",
    "product size": "dimensions",
    "dimensions mm": "dimensions",
    "product weight": "weight",
    "net weight": "weight",
    "battery capacity": "capacity",
    "communication interface": "interface",
    "interface type": "interface",
}

_NUMBER_RE = re.compile(
    r"^([+-]?\d+(?:\.\d+)?)\s*([a-zA-Z%µ°Ω][a-zA-Z0-9%/\.\-]*)?$"
)
_PAIR_RE = re.compile(
    r"^\s*[-•*]?\s*([A-Za-z][A-Za-z0-9 /()\-\.&]{1,45}?)\s*:\s*(.+?)\s*$"
)
_WS_RE = re.compile(r"\s+")


def clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    text = html_lib.unescape(str(value))
    text = text.replace("\xa0", " ").replace("\u200b", "")
    return _WS_RE.sub(" ", text).strip()


def normalize_key(label: str) -> str:
    """Lower-case snake_case key used for grouping vendor attributes."""
    raw = clean_text(label).lower()
    raw = _KEY_ALIASES.get(raw, raw)
    key = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return key[:80]


def _numeric(value: str) -> Tuple[Optional[float], Optional[str]]:
    """Returns (number, unit) only for a single scalar; ranges stay strings."""
    match = _NUMBER_RE.match(value)
    if not match:
        return None, None
    try:
        number = float(match.group(1))
    except (TypeError, ValueError):
        return None, None
    unit = (match.group(2) or "").strip().lower() or None
    return number, unit


def _acceptable(key: str, value: str) -> bool:
    if not key or not value:
        return False
    if key in _SKIP_KEYS:
        return False
    if len(key) > 60 or len(value) > 350:
        return False
    if not value.strip(" -–—|:;,."):
        return False
    if len(key.split()) > 7:
        return False
    if "http://" in value.lower() or "https://" in value.lower():
        return False
    if value.startswith("//"):
        return False
    if key in {"http", "https", "www", "url", "link"}:
        return False
    # A key that is mostly digits (a pin number row) is table structure, not a spec.
    if sum(ch.isdigit() for ch in key) > len(key) / 2:
        return False
    return True


def _add(
    out: Dict[str, Dict[str, Any]],
    label: str,
    value: str,
    source: str,
    confidence: float,
) -> None:
    key = normalize_key(label)
    value = clean_text(value)
    if not _acceptable(key, value):
        return
    number, unit = _numeric(value)
    existing = out.get(key)
    if existing is not None and existing["confidence"] >= confidence:
        return
    out[key] = {
        "key": key,
        "label": clean_text(label)[:120],
        "value": value,
        "value_num": number,
        "unit": unit,
        "source": source,
        "confidence": confidence,
    }


def _table_pairs(soup: BeautifulSoup) -> Iterable[Tuple[str, str]]:
    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"])
            if len(cells) != 2:
                continue
            key = clean_text(cells[0].get_text(" "))
            value = clean_text(cells[1].get_text(" "))
            if not key or not value:
                continue
            if (
                key.lower() in _HEADER_KEYS
                and value.lower() in _HEADER_VALUES
            ):
                continue
            yield key, value


def _block_pairs(soup: BeautifulSoup) -> Iterable[Tuple[str, str]]:
    """List/paragraph blocks keep inline <strong>Key:</strong> and value together."""
    for node in soup.find_all(["li", "p", "dd"]):
        text = clean_text(node.get_text(" "))
        if not text or len(text) > 400:
            continue
        match = _PAIR_RE.match(text)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if not key or not value:
            continue
        if key.lower().split()[0] in {"it", "this", "that", "the", "these", "those"}:
            continue
        yield key, value


def _text_pairs(text: str) -> Iterable[Tuple[str, str]]:
    for line in text.splitlines():
        line = clean_text(line)
        if not line or len(line) > 400:
            continue
        match = _PAIR_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if not key or not value:
            continue
        # Avoid sentence fragments like "It is: recommended".
        if key.lower().split()[0] in {"it", "this", "that", "the", "these", "those"}:
            continue
        yield key, value


def description_text(description_html: Optional[str], limit: int = 20000) -> str:
    """Readable plain text for `get_product.description`."""
    if not description_html:
        return ""
    soup = BeautifulSoup(description_html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [clean_text(line) for line in soup.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:limit]


def extract_description_attributes(
    description_html: Optional[str],
    max_attributes: int = 80,
) -> List[Dict[str, Any]]:
    """
    Extracts normalized key/value attributes from a vendor description.

    Tables win over prose when the same key appears twice. The result is a list
    ordered by first appearance; callers can persist it directly.
    """
    if not description_html:
        return []
    soup = BeautifulSoup(description_html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    attributes: Dict[str, Dict[str, Any]] = {}
    for key, value in _table_pairs(soup):
        _add(attributes, key, value, "description_table", 0.95)
        if len(attributes) >= max_attributes:
            break

    if len(attributes) < max_attributes:
        for key, value in _block_pairs(soup):
            _add(attributes, key, value, "description_text", 0.8)
            if len(attributes) >= max_attributes:
                break

    if len(attributes) < max_attributes:
        text = soup.get_text("\n")
        for key, value in _text_pairs(text):
            _add(attributes, key, value, "description_text", 0.75)
            if len(attributes) >= max_attributes:
                break

    return list(attributes.values())
