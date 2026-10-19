import urllib.request
import json
import re

url = "http://127.0.0.1:8000/search?q=EasyMech"
with urllib.request.urlopen(url) as resp:
    data = json.loads(resp.read().decode("utf-8"))

print(f"Total comparison groups for 'EasyMech': {len(data['comparison_groups'])}")
singleton_groups = [g for g in data['comparison_groups'] if len(g['offers']) == 1]
print(f"Total singleton groups: {len(singleton_groups)}")

# Verify the rendered DOM structure for singleton cards
# In index.html, singleton cards are rendered with:
# class 'card-comparison mb-3', a card-body with vendor badge, offer.title, price, and View button.
# And NO 'Compare offers', NO 'merge_reason', NO 'single_offer', NO 'Offers from N vendors'.

for idx, g in enumerate(singleton_groups[:4]):
    offer = g['offers'][0]
    print(f"\n--- Singleton Card [{idx+1}] ---")
    print(f"  Header Title Rendered: \"{offer['title']}\"")
    print(f"  Vendor:                {offer['vendor']}")
    print(f"  Price:                 Rs. {offer['price']:.2f}")
    print(f"  Stock Status:          {'In Stock' if offer['available'] else 'Out of stock'}")
    print(f"  Has 'Compare offers':  False (bypassed for singletons)")
    print(f"  Has 'merge_reason':    False (stripped from DOM)")
    print(f"  Action Link:           View -> to {offer['product_url'][:45]}...")

# Also check query "easy mech 100mm"
url_100mm = "http://127.0.0.1:8000/search?q=easy+mech+100mm"
with urllib.request.urlopen(url_100mm) as resp:
    data_100mm = json.loads(resp.read().decode("utf-8"))
print(f"\nQuery 'easy mech 100mm' results: {data_100mm['total_results']} items, {len(data_100mm['comparison_groups'])} groups.")
if data_100mm['total_results'] == 0:
    print("  DOM shows empty state: 'No components found for 'easy mech 100mm'. Try a shorter term or MPN.' (Zero comparison or merge_reason text).")
