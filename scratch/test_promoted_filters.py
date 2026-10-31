import sys
from pathlib import Path

sys.path.insert(0, ".")
import db

db.settings.db_path = Path("data/components_copy.db")

from mcp_server.server import count_components, search_components

cases = [
    ("resistors", {"resistance_ohm": {"gte": 1000, "lte": 10000}, "power_w": {"gte": 0.25}}),
    ("capacitors", {"capacitance_uf": {"gte": 100}}),
    ("integrated-circuits", {"package": "DIP", "pins": {"gte": 8}}),
    ("connectors", {"pins": {"gte": 20}, "pitch_mm": {"gte": 2.0}}),
    ("leds", {"color": "red"}),
    ("relay", {"channels": {"gte": 4}}),
]
for category, filters in cases:
    c = count_components(category=category, filters=filters)
    s = search_components(category=category, filters=filters, limit=3, refresh=False)
    print(category, filters, "count", c.ok, c.total_matches, "search", s.ok, s.total_matches)
    if s.ok:
        for row in s.data["results"][:2]:
            print("  ", row["specs"], row["title"][:60])
