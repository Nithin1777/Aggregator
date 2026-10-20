import re
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
url = "https://robokits.co.in/motors/hybrid-servo-motors/hybrid-servo-with-gearbox/nema34-hybrid-servo-motor-with-worm-gearbox-180kgcm"
r = creq.get(url, impersonate="chrome120", headers=H, timeout=30)
print("status", r.status_code, "len", len(r.text))
html = r.text
soup = BeautifulSoup(html, "html.parser")
print("title:", soup.title.get_text(strip=True) if soup.title else None)
for pat in ("Specification", "specification", "Technical", "Feature", "Additional"):
    print(f"  {pat}:", len(re.findall(pat, html)))
tables = soup.find_all("table")
print("tables:", len(tables))
for t in tables[:4]:
    txt = re.sub(r"\s+", " ", t.get_text(" | ", strip=True))
    print("  table:", txt[:500])
# look for spec-ish definition lists / list items
for h in soup.find_all(["h2", "h3", "h4", "strong", "b"]):
    txt = h.get_text(" ", strip=True)
    if re.search(r"spec|feature|technical", txt, re.I):
        print("HEADING:", txt[:100])
