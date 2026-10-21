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
      price { regular final special discount }
      in_stock
      is_backorder
      available_quantity
      categories { id name url_path }
      images { url }
    }
  }
}"""

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
    json={"operationName": "ProductDetails", "query": QUERY, "variables": {"slug": "raspberry-pi-pico"}},
    impersonate="chrome120",
    timeout=30,
)
print("status", r.status_code, "len", len(r.text))
data = r.json()
print(json.dumps(data, indent=1)[:4000])
