import urllib.request
import json
import urllib.parse

queries = ["Smart elex", "easy mech wheels", "Astro Coupler"]
print("--- STEP 4 SEARCH RECALL VERIFICATION ---")
for q in queries:
    url = f"http://127.0.0.1:8000/search?q={urllib.parse.quote(q)}"
    with urllib.request.urlopen(url) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        total = data.get("total_results", 0)
        groups = data.get("comparison_groups", [])
        top_title = groups[0]["canonical_name"] if groups else "NO_RESULTS"
        match_mode = data.get("source", "N/A")
        print(f"Query: '{q}'")
        print(f"  HTTP Status: {resp.status}")
        print(f"  Match Mode:  {match_mode}")
        print(f"  Total Items: {total}")
        print(f"  Top Result:  \"{top_title}\"")
        if total == 0:
            print("  -> REGRESSION: 0 results returned!")
        print()
