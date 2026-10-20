"""Measure how much structured spec data exists in Shopify descriptions (no DB writes)."""
import re
from collections import Counter

from curl_cffi import requests as creq

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", s).strip()


for name, base in (("quartzcomponents", "https://quartzcomponents.com"),
                   ("makerbazar", "https://makerbazar.in"),
                   ("rees52", "https://rees52.com")):
    products = []
    for page in (1, 2):
        r = creq.get(f"{base}/products.json", params={"limit": 250, "page": page},
                     impersonate="chrome120", headers=H, timeout=40)
        products.extend(r.json().get("products", []))
    with_table = 0
    rows_total = 0
    keys = Counter()
    samples = 0
    for it in products:
        body = it.get("body_html") or ""
        tables = re.findall(r"<table.*?</table>", body, re.I | re.S)
        if not tables:
            continue
        with_table += 1
        for t in tables:
            trs = re.findall(r"<tr.*?</tr>", t, re.I | re.S)
            for tr in trs:
                cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.I | re.S)
                if len(cells) == 2:
                    k, v = clean(cells[0]).lower(), clean(cells[1])
                    if k and v and len(k) < 40:
                        rows_total += 1
                        keys[k] += 1
        samples += 1
    print(f"\n{name}: fetched {len(products)}, with_table={with_table} ({100*with_table/max(len(products),1):.0f}%), kv_rows={rows_total}, distinct_keys={len(keys)}")
    print("  top keys:", keys.most_common(12))
