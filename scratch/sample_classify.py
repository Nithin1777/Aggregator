import random
import sqlite3
import sys

sys.path.insert(0, ".")
from src.spec.extractor import classify_category
from src.spec.taxonomy_loader import get_taxonomy

con = sqlite3.connect("data/components.db")
rows = con.execute("select title from products order by random() limit 3000").fetchall()
tax = get_taxonomy()
from collections import Counter
c = Counter()
none = 0
for (t,) in rows:
    r = classify_category(t or "", tax)
    if r:
        c[r[0]] += 1
    else:
        none += 1
print("sampled", len(rows), "classified", len(rows) - none, f"{100*(len(rows)-none)/len(rows):.1f}%", "none", none)
print(c.most_common(20))
