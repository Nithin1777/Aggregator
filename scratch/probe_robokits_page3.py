import re
from bs4 import BeautifulSoup
from curl_cffi import requests as creq

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
url = "https://robokits.co.in/motors/hybrid-servo-motors/hybrid-servo-with-gearbox/nema34-hybrid-servo-motor-with-worm-gearbox-180kgcm"
r = creq.get(url, impersonate="chrome120", headers=H, timeout=30)
html = r.text
soup = BeautifulSoup(html, "html.parser")
for m in re.finditer("Specification", html):
    seg = html[m.start() - 300:m.start() + 1200]
    txt = re.sub(r"<[^>]+>", " ", seg)
    print("---", re.sub(r"\s+", " ", txt)[:700])
print("\n=== product description block ===")
for div in soup.find_all(["div", "section"], id=re.compile(r"desc|tab|product", re.I), limit=20):
    txt = div.get_text(" ", strip=True)
    if len(txt) > 200 and re.search(r" specification| voltage| current| torque| weight", txt, re.I):
        print(div.get("id"), "::", txt[:1000])
        break
