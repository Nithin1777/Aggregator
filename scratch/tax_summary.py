import yaml

d = yaml.safe_load(open("config/taxonomy.yaml", encoding="utf-8"))
cats = d["categories"]
print("categories", len(cats))
total = 0
for c in cats:
    attrs = c.get("attributes") or []
    total += len(attrs)
    keys = [a["key"] for a in attrs]
    print(f"{c['id']:<26} attrs={len(attrs):<2} {keys}")
print("attributes total", total)
