import json
import sys

sys.path.insert(0, ".")
from curl_cffi import requests as creq

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://robu.in",
    "Referer": "https://robu.in/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def gql(query, variables=None):
    r = creq.post(
        "https://robu.in/api/proxy/graphql/",
        headers=HEADERS,
        json={"query": query, "variables": variables or {}},
        impersonate="chrome120",
        timeout=30,
    )
    print("status", r.status_code)
    return r.json()


for type_name in ("ProductDetail", "ProductPrice"):
    data = gql(
        """query Introspect($name: String!) {
          __type(name: $name) {
            name
            fields { name type { kind name ofType { kind name } } }
          }
        }""",
        {"name": type_name},
    )
    print("\n==", type_name)
    print(json.dumps(data, indent=1)[:3000])
