import re
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

r = creq.get(
    "https://robokits.co.in/index.php?main_page=advanced_search_result&search_in_description=1&keyword=raspberry+pi+pico",
    impersonate="chrome120", headers=H, timeout=30,
)
print("search", r.status_code, len(r.text))
soup = BeautifulSoup(r.text, "html.parser")
links = []
for a in soup.find_all("a", href=True):
    href = a["href"]
    if "products_id=" in href and "product" in href.lower():
        links.append((href, a.get_text(" ", strip=True)[:70]))
seen = set()
uniq = []
for href, text in links:
    key = re.sub(r"&amp;.*", "", href)
    if key not in seen:
        seen.add(key)
        uniq.append((href, text))
print("links", len(uniq))
for href, text in uniq[:5]:
    print(" ", text, "->", href[:120])
if uniq:
    url = uniq[0][0]
    if url.startswith("/"):
        url = "https://robokits.co.in" + url
    pr = creq.get(url, impersonate="chrome120", headers=H, timeout=30)
    print("\nproduct", pr.status_code, len(pr.text))
    html = pr.text
    for pat in ("Specification", "specification", "Technical", "Features"):
        print(f"  {pat}:", len(re.findall(pat, html)))
    print("  tables:", len(re.findall(r"<table", html, re.I)))
    m = re.search(r"specification", html, re.I)
    if m:
        seg = re.sub(r"<[^>]+>", " | ", html[m.start():m.start() + 3000])
        print("  context:", re.sub(r"\s+", " ", seg)[:1200])
