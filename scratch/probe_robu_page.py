import re
from curl_cffi import requests as creq

H = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
s = creq.Session(impersonate="chrome120", headers=H)
r0 = s.get("https://robu.in/", timeout=20)
print("warmup", r0.status_code)
r = s.get("https://robu.in/product/raspberry-pi-pico/", timeout=30)
print("product", r.status_code, "len", len(r.text))
html = r.text
for pat in ("Specification", "specification", "Additional information", "Technical Details", "attr"):
    print(f"  {pat!r}:", len(re.findall(pat, html)))
tables = re.findall(r"<table.*?</table>", html, re.I | re.S)
print("tables:", len(tables))
for t in tables[:3]:
    txt = re.sub(r"<[^>]+>", " | ", t)
    print("   ", re.sub(r"\s+", " ", txt)[:600])
m = re.search(r"specification", html, re.I)
if m:
    print("context:", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[m.start() - 200:m.start() + 1500]))[:1200])
# look for JSON-LD
for j in re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)[:3]:
    print("JSONLD:", j[:500])
