import re
from curl_cffi import requests as creq

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

for base in ("https://quartzcomponents.com", "https://makerbazar.in", "https://rees52.com"):
    r = creq.get(f"{base}/products.json", params={"limit": 6, "page": 1}, impersonate="chrome120", headers=H, timeout=30)
    items = r.json().get("products", [])
    print("\n\n=========", base, "=========")
    for it in items[:3]:
        body = it.get("body_html") or ""
        print("\n---", it.get("title", "")[:80])
        tables = re.findall(r"<table.*?</table>", body, re.I | re.S)
        print("tables:", len(tables))
        for t in tables[:1]:
            print(re.sub(r"\s+", " ", t)[:1500])
        if not tables:
            text = re.sub(r"<[^>]+>", "\n", body)
            text = re.sub(r"\n\s*\n+", "\n", text)
            print("TEXT:", text[:800])
