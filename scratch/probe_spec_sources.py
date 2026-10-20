"""Probe live vendor payloads to see whether specs are actually provided."""
import json
import re
import sys

sys.path.insert(0, ".")

from curl_cffi import requests as creq

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

SPEC_HINTS = re.compile(
    r"specification|technical details|features|parameters|attributes|"
    r"operating voltage|rated voltage|power rating|tolerance|"
    r"current|torque|capacity|resolution|interface",
    re.I,
)


def probe_shopify(name, base):
    print(f"\n=== Shopify: {name} ({base}) ===")
    try:
        r = creq.get(
            f"{base}/products.json",
            params={"limit": 5, "page": 1},
            impersonate="chrome120",
            headers=H,
            timeout=30,
        )
    except Exception as e:
        print("  fetch failed:", e)
        return
    print("  status", r.status_code)
    if r.status_code != 200:
        return
    items = r.json().get("products", [])
    print("  products:", len(items))
    for it in items:
        body = it.get("body_html") or ""
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()
        tables = len(re.findall(r"<table", body, re.I))
        print(
            f"  - {it.get('title','')[:58]!r}\n"
            f"      body_html={len(body)} chars, tables={tables}, "
            f"spec_hint={bool(SPEC_HINTS.search(text))}, text[:120]={text[:120]!r}"
        )
    print("  item keys:", sorted(items[0].keys()) if items else None)


def probe_robu():
    print("\n=== Robu search API ===")
    try:
        r = creq.post(
            "https://robu.in/api/search/",
            headers={**H, "Content-Type": "application/json", "Origin": "https://robu.in", "Referer": "https://robu.in/"},
            json={"q": "motor", "limit": 3, "offset": 0, "semantic": False, "sort": ""},
            impersonate="chrome120",
            timeout=30,
        )
    except Exception as e:
        print("  fetch failed:", e)
        return
    print("  status", r.status_code)
    if r.status_code != 200:
        print("  body:", r.text[:300])
        return
    data = r.json()
    prods = data.get("data", {}).get("products", [])
    print("  products:", len(prods))
    if prods:
        p = prods[0]
        print("  item keys:", sorted(p.keys()))
        for k in sorted(p.keys()):
            v = p[k]
            s = json.dumps(v, default=str)
            if any(t in k.lower() for t in ("attr", "spec", "desc", "feature", "detail", "meta")):
                print(f"    {k}: {s[:400]}")


def probe_robokits():
    print("\n=== Robokits product page ===")
    # find a product URL from the search page
    try:
        r = creq.get(
            "https://robokits.co.in/index.php?main_page=advanced_search_result&search_in_description=1&keyword=raspberry+pi+pico",
            impersonate="chrome120",
            headers=H,
            timeout=30,
        )
    except Exception as e:
        print("  search fetch failed:", e)
        return
    print("  search status", r.status_code, "len", len(r.text))
    links = re.findall(r'href="(https://robokits\.co\.in/[^"]*product[^"]*)"', r.text, re.I)
    print("  product links:", len(links), links[:3])
    if not links:
        return
    pr = creq.get(links[0], impersonate="chrome120", headers=H, timeout=30)
    print("  product status", pr.status_code, "len", len(pr.text))
    html = pr.text
    print("  has 'Specification':", bool(re.search(r"specification", html, re.I)))
    print("  tables:", len(re.findall(r"<table", html, re.I)))
    idx = re.search(r"specification", html, re.I)
    if idx:
        print("  context:", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[idx.start():idx.start()+900]))[:600])


probe_shopify("quartzcomponents", "https://quartzcomponents.com")
probe_shopify("makerbazar", "https://makerbazar.in")
probe_shopify("rees52", "https://rees52.com")
probe_robu()
probe_robokits()
