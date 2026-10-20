import sqlite3

con = sqlite3.connect("data/components.db")
con.row_factory = sqlite3.Row
expr = "replace(replace(replace(upper(trim(coalesce(mpn,''))), '-', ''), ' ', ''), '_', '')"
for needle in ("ESP32", "MPU6050", "GB37"):
    n = con.execute(
        f"select count(*) c, count(distinct title) t, count(distinct vendor) v, count(distinct mpn) m from products where {expr}=?",
        (needle,),
    ).fetchone()
    print(needle, dict(n))
    if needle == "ESP32":
        rows = con.execute(
            f"select title, vendor_name, mpn from products where {expr}=? limit 8", (needle,)
        ).fetchall()
        for r in rows:
            print("   ", r["vendor_name"], "|", r["title"][:70], "| mpn=", r["mpn"])
