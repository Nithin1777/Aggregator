"""User-style search evaluation: run many queries through the live API and flag
repeated ranking failures. This is an investigation harness, not a unit test."""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

BASE = "http://127.0.0.1:8000"
OUT = Path(__file__).resolve().parent / "eval_results.json"

ACCESSORY = re.compile(
    r"\b(brackets?|mount(?:ing)?|holders?|stands?|screws?|nuts?|bolts?|"
    r"brushes?|brush|replacement|spare|case|cover|enclosure|housing|cable|"
    r"wire|cord|adapter|connector|heatsink|heat\s*sink|fan|pad|belt|pulley|"
    r"horn|arm|wheel|hub|shaft|coupling|bearing|gear\s*box|gearbox|"
    r"for\s+\S+|compatible\s+with|moq|pack\s+of|set\s+of)\b",
    re.IGNORECASE,
)
CORE = re.compile(
    r"\b(motor|board|sensor|module|driver|relay|servo|battery|cell|display|"
    r"screen|ic|chip|controller|converter|supply|charger|switch|transistor|"
    r"mosfet|diode|capacitor|resistor|inductor|crystal|antenna|laser|pump|"
    r"valve|encoder|inverter|amplifier|receiver|transmitter|microcontroller|"
    r"development\s+board|dev\s*board|breakout)\b",
    re.IGNORECASE,
)
ACCESSORY_QUERY = re.compile(
    r"\b(bracket|mount|holder|stand|screw|brush|case|cover|cable|wire|adapter|"
    r"connector|heatsink|fan|pad|belt|pulley|horn|arm|wheel|hub|shaft|coupling|"
    r"bearing|gearbox|spare|replacement)\b",
    re.IGNORECASE,
)


def api(query: str, page_size: int = 10) -> dict:
    params = urllib.parse.urlencode(
        {"q": query, "source": "local", "page": 1, "page_size": page_size, "refresh": "off"}
    )
    url = f"{BASE}/api/search?{params}"
    t0 = time.perf_counter()
    with urllib.request.urlopen(url, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    payload["_ms"] = round((time.perf_counter() - t0) * 1000)
    return payload


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def query_tokens(query: str) -> set[str]:
    stop = {"the", "for", "with", "and", "of", "in", "on", "a", "an", "to", "under", "below", "over", "above", "best"}
    return {t for t in tokens(query) if t not in stop and len(t) > 1}


def flags_for(query: str, data: dict) -> list[str]:
    groups = data.get("comparison_groups") or []
    flags: list[str] = []
    if not groups:
        return ["empty"]

    qt = query_tokens(query)
    titles = [g.get("canonical_name") or "" for g in groups]
    top = titles[0]
    top_tokens = tokens(top)
    top_is_accessory = bool(ACCESSORY.search(top)) and not ACCESSORY_QUERY.search(query)
    top_is_core = bool(CORE.search(top))

    # The core item exists lower down but an accessory is first.
    if top_is_accessory:
        for idx, title in enumerate(titles[1:10], start=1):
            if CORE.search(title) and (qt & tokens(title)):
                flags.append(f"accessory_first(core_at_{idx + 1})")
                break

    # Exact model/MPN query: top 3 should contain the query token in a primary position.
    if len(qt) == 1 and re.search(r"\d", next(iter(qt))):
        token = next(iter(qt))
        top3_ok = False
        for title in titles[:3]:
            low = title.lower()
            pos = low.find(token)
            if pos >= 0 and not re.search(rf"(for|compatible\s+with|fits|used\s+with)\s+[^.]*\b{re.escape(token)}\b", low):
                top3_ok = True
                break
        if not top3_ok:
            flags.append("model_not_primary_top3")

    # Multi-token query where the top card misses query tokens but a later card has them.
    if len(qt) > 1:
        missing = qt - top_tokens
        if missing:
            for idx, title in enumerate(titles[1:10], start=1):
                if qt <= tokens(title):
                    flags.append(f"coverage_miss(tokens={','.join(sorted(missing))},better_at_{idx + 1})")
                    break

    # Two near-identical cards that were never merged.
    for i in range(min(3, len(titles))):
        for j in range(i + 1, min(4, len(titles))):
            a, b = tokens(titles[i]), tokens(titles[j])
            if a and b and len(a & b) / len(a | b) > 0.9:
                flags.append(f"near_duplicate_cards({i + 1},{j + 1})")
                break

    # Generic title with no specific model token from the query.
    if top_is_core and qt and not (qt & top_tokens):
        flags.append("generic_top")

    return flags


def build_queries() -> list[str]:
    conn = db.get_db_connection()
    queries: list[str] = []
    try:
        # High-frequency MPNs (the parts users actually look up).
        rows = conn.execute(
            """
            SELECT mpn, COUNT(*) c FROM products
            WHERE mpn IS NOT NULL AND length(mpn) >= 3
            GROUP BY mpn ORDER BY c DESC LIMIT 45;
            """
        ).fetchall()
        queries += [r[0] for r in rows]

        # Canonical seed keys.
        import csv
        with open("seeds/canonical_products.csv", encoding="utf-8") as fh:
            queries += [r["key"] for r in csv.DictReader(fh)]
    finally:
        conn.close()

    # Category / brand terms and natural-language requests.
    queries += [
        "servo", "relay", "stepper", "buck converter", "sensor", "oled", "dc motor",
        "lithium battery", "bms", "arduino", "esp32", "raspberry pi", "stm32",
        "rp2040", "esp8266", "mosfet", "igbt", "thyristor", "inductor", "crystal",
        "capacitor", "resistor", "diode", "led", "display", "connector", "switch",
        "motor driver", "motor bracket", "servo horn", "battery holder",
        "12v geared motor", "24v 100w motor", "nema17 stepper", "sg90 servo",
        "raspberry pi pico", "raspberry pi 5", "arduino uno", "esp32 cam",
        "battery for esp32 weather station", "12v motor under 500",
        "resistor 10k", "capacitor 100uf", "oled 0.96", "relay 5v 2 channel",
        "dc motor 60rpm", "motor 24v 100w", "ig52", "nema17", "sg90", "dht11",
        "lm2596", "mpu6050", "bme280", "tp4056", "l298n", "hc-sr04", "ne555",
        "ams1117", "xl4015", "mt3608", "max6675", "max30102", "ina219",
    ]

    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        q = (q or "").strip()
        if q and q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    return out


def main() -> None:
    queries = build_queries()
    print(f"running {len(queries)} queries against {BASE}")
    results = []
    for i, query in enumerate(queries, start=1):
        try:
            data = api(query)
            flags = flags_for(query, data)
            groups = data.get("comparison_groups") or []
            results.append({
                "query": query,
                "ms": data["_ms"],
                "total": data.get("total_results"),
                "groups": [
                    {
                        "title": g.get("canonical_name"),
                        "offers": g.get("total_offers"),
                        "instock": g.get("in_stock_count"),
                        "best": g.get("best_price"),
                        "mpn": g.get("mpn"),
                        "category": g.get("category_canonical"),
                    }
                    for g in groups[:10]
                ],
                "flags": flags,
            })
        except Exception as exc:  # noqa: BLE001 - investigation harness
            results.append({"query": query, "error": str(exc), "flags": ["error"]})

        if i % 20 == 0:
            print(f"  {i}/{len(queries)}...")

    OUT.write_text(json.dumps(results, indent=2), encoding="utf-8")

    flagged = [r for r in results if r.get("flags")]
    print(f"\n{len(results)} queries, {len(flagged)} flagged")
    pattern_counts = Counter()
    for item in flagged:
        for flag in item["flags"]:
            pattern_counts[flag.split("(")[0]] += 1
    print("pattern counts:", dict(pattern_counts))
    print("\n--- sample flagged queries ---")
    for item in flagged[:30]:
        print(f"\n[{item['query']}] {item['flags']} ({item['ms']} ms, total={item.get('total')})")
        for idx, group in enumerate(item.get("groups", [])[:4], start=1):
            print(f"  {idx}. {group['title'][:95]} | offers={group['offers']} best={group['best']}")


if __name__ == "__main__":
    main()
