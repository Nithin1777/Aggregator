import json
import sys

sys.path.insert(0, ".")
from curl_cffi import requests as creq

FIELDS = """
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
      price { regular final special discount }
      in_stock
      is_backorder
      available_quantity
      categories { id name url_path }
      images { url }
    }
"""
slugs = ["raspberry-pi-pico", "raspberry-pi-pico-2"]
parts = []
variables = {}
for i, slug in enumerate(slugs):
    parts.append(f'p{i}: productdetails(slug: $s{i}) {{{FIELDS}}}')
    variables[f"s{i}"] = slug
query = "query Batch(" + ", ".join(f"$s{i}: String!" for i in range(len(slugs))) + ") {\n" + "\n".join(parts) + "\n}"

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
    json={"operationName": "Batch", "query": query, "variables": variables},
    impersonate="chrome120",
    timeout=60,
)
print("status", r.status_code, "len", len(r.text))
data = r.json()
print(json.dumps(data, indent=1)[:1500])
for i in range(len(slugs)):
    p = (data.get("data") or {}).get(f"p{i}") or {}
    d = p.get("data") or {}
    print(i, p.get("status"), d.get("name"), d.get("price"), d.get("in_stock"), len(d.get("description") or ""))
