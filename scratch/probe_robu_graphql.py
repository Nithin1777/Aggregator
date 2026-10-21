import json
import sys

sys.path.insert(0, ".")
from curl_cffi import requests as creq

QUERY = """query ProductDetails($slug: String!, $preview_token: String) {
  productdetails(slug: $slug, preview_token: $preview_token) {
    status
    type
    code
    message
    data {
      id
      sku
      type
      name
      url_key
      short_description
      description
      price
      special_price
      in_stock
      is_backorder
      categories { id name url_path }
      images { url }
    }
  }
}"""

payload = {
    "operationName": "ProductDetails",
    "query": QUERY,
    "variables": {"slug": "raspberry-pi-pico"},
}
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://robu.in",
    "Referer": "https://robu.in/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
r = creq.post(
    "https://robu.in/api/proxy/graphql/",
    headers=headers,
    json=payload,
    impersonate="chrome120",
    timeout=30,
)
print("status", r.status_code, "len", len(r.text))
print(r.text[:600])
if r.status_code == 200:
    try:
        data = r.json()
        product = (data.get("data") or {}).get("productdetails") or {}
        p = product.get("data") or {}
        print("keys", sorted(p.keys()))
        print("name", p.get("name"))
        print("short", (p.get("short_description") or "")[:300])
        print("desc", (p.get("description") or "")[:600])
    except Exception as e:
        print("json fail", e)
