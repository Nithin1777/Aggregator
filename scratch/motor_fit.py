import re
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
c = sqlite3.connect("data/components.db")
c.row_factory = sqlite3.Row
cur = c.cursor()

rows = cur.execute(
    "select p.id, p.vendor_name, p.title, p.price, p.available, p.product_url, "
    "       p.category, p.raw_json "
    "from products p join product_specs s on s.product_id = p.id "
    "where s.key = 'voltage' and s.value = '24' "
    "  and p.price between 3800 and 7500"
).fetchall()


def torque_kgcm(title):
    t = title.lower().replace(" ", "")
    m = re.search(r"(\d+(?:\.\d+)?)kg-?c?m", t)
    if m:
        return float(m.group(1))
    return None


cands = []
for r in rows:
    tq = torque_kgcm(r["title"])
    rp = re.search(r"(\d+)rpm", r["title"], re.I)
    cands.append((r, tq, int(rp.group(1)) if rp else None))

print("=== 24V motors 3.8k-7.5k with detectable torque >=140 kg-cm\n")
sel = [c for c in cands if c[1] and c[1] >= 140]
sel.sort(key=lambda x: x[0]["price"])
for r, tq, rp in sel:
    print(f"Rs {r['price']:>6.0f} | {'IN ' if r['available'] else 'OUT'} | {tq:>5.0f} kg-cm | "
          f"{str(rp)+'rpm' if rp else '?':>7} | {r['vendor_name']}")
    print(f"    {r['title'][:100]}")
    print(f"    {r['product_url']}")
    print(f"    category={r['category']}")

print("\n\n=== Any catalog text mentioning keyway / 12mm shaft / M5 PCD / PG52 / IG52?\n")
for term in ["keyway", "key way", "pcd", "m5", "12mm shaft", "12 mm shaft", "ig52", "pg52", "ig55", "52mm"]:
    n = cur.execute(
        "select count(*) from products where lower(title) like ?", (f"%{term}%",)
    ).fetchone()[0]
    print(f"  title contains {term!r:>14}: {n}")

print("\n\n=== raw_json of top IG52 160kgcm listing (field check)\n")
r = cur.execute(
    "select title, raw_json from products where title like '%IG52%160KGCM%' limit 1"
).fetchone()
if r:
    print(r["title"])
    rj = r["raw_json"] or ""
    print("raw_json length:", len(rj))
    print(rj[:1200])
