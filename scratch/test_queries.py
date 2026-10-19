import urllib.request
import json
import urllib.parse

def check_search_endpoint(query):
    url = f"http://127.0.0.1:8000/search?q={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            print(f"=== Query: '{query}' ===")
            print(f"Status: {resp.status}")
            print(f"Total results: {data.get('total_results')}")
            print(f"Comparison groups: {len(data.get('comparison_groups', []))}")
            for idx, g in enumerate(data.get("comparison_groups", [])):
                print(f"  Group [{idx+1}]: \"{g.get('canonical_name')}\" (offers: {len(g.get('offers', []))})")
                for o in g.get("offers", []):
                    print(f"     - {o.get('vendor')}: \"{o.get('title')}\" (Rs. {o.get('price')})")
            print()
    except Exception as e:
        print(f"=== Query: '{query}' FAILED: {e} ===")

check_search_endpoint("easy mech wheels")
check_search_endpoint("EasyMech")

