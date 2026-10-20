"""Report on how the canonical taxonomy maps onto the real catalog."""
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, ".")
from src.catalog.taxonomy import classify, load_taxonomy

OUT = Path("scratch/taxonomy_report.txt")
conn = sqlite3.connect("data/components.db")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA temp_store=MEMORY")

taxonomy = load_taxonomy()
rows = conn.execute("SELECT id, category, title, spec_category FROM products").fetchall()

counts = Counter()
samples = defaultdict(list)
other_raw = Counter()
for r in rows:
    slug, kw = classify(r["category"], r["title"], r["spec_category"])
    counts[slug] += 1
    if slug == "other":
        other_raw[str(r["category"])] += 1
    if len(samples[slug]) < 8:
        samples[slug].append((r["category"], r["title"][:80], kw))

total = sum(counts.values())
lines = []
lines.append(f"total={total}\n")
for cat in taxonomy.categories:
    n = counts.get(cat.slug, 0)
    lines.append(f"{n:>7}  {n*100/total:5.1f}%  {cat.slug:<20} {cat.label}")

lines.append("\n=== SAMPLES ===")
for cat in taxonomy.categories:
    lines.append(f"\n-- {cat.slug} ({counts.get(cat.slug,0)})")
    for raw, title, kw in samples[cat.slug]:
        lines.append(f"     kw={kw!r:<26} raw={str(raw)[:44]!r:<48} | {title}")

lines.append("\n=== RAW CATEGORIES LANDING IN 'other' (top 60) ===")
for raw, n in other_raw.most_common(60):
    lines.append(f"  {n:>6}  {raw!r}")

OUT.write_text("\n".join(lines), encoding="utf-8")
print("wrote", OUT, "total", total)
conn.close()
