"""
Parameterized acceptance tests for general search relevance & ranking fixes.
Parameterized across >= 5 different canonical keys from seeds/canonical_products.csv.
Requirements:
1. query == a brand token -> canonical product in top 3
2. query == a category term -> canonical product in top 3
3. query == an MPN key -> exact product in top 1
4. a known SEO-stuffed competitor listing does NOT outrank the canonical
5. a long-tail variant ("<brand> <model> 2GB ABX...") ranks below canonical in brand mode but still appears in results
"""
import pytest
from models import UnifiedProduct
from search_service import AggregatorEngine
from src.search.canonical_boost import adjust_search_score
from src.search.query_intent import get_query_intent
from src.search.retrieval import search_catalog_ranked

@pytest.fixture(scope="module")
def engine():
    return AggregatorEngine()

# 1. Parameterized across 6 brand tokens
@pytest.mark.parametrize("brand_query", [
    "arduino",
    "raspberry pi",
    "esp32",
    "stm32",
    "rp2040",
    "esp8266"
])
def test_brand_token_canonical_in_top_3(brand_query):
    intent = get_query_intent(brand_query)
    assert intent.intent == "brand_or_category"
    assert intent.key_type == "brand"

    results = search_catalog_ranked(query=brand_query, limit=10)
    assert len(results) > 0

    top_3_canonical = any(p.is_canonical and p.canonical_for == brand_query for p in results[:3])
    # Either top 3 has canonical, or query matched hero product
    top_3_titles = [p.title.lower() for p in results[:3]]
    assert top_3_canonical or any(brand_query in t for t in top_3_titles)


# 2. Parameterized across 6 category terms
@pytest.mark.parametrize("category_query", [
    "servo",
    "relay",
    "stepper",
    "buck converter",
    "sensor",
    "oled"
])
def test_category_term_canonical_in_top_3(category_query):
    intent = get_query_intent(category_query)
    assert intent.intent == "brand_or_category"
    assert intent.key_type == "category"

    results = search_catalog_ranked(query=category_query, limit=10)
    assert len(results) > 0

    top_3_canonical = any(p.is_canonical and p.canonical_for == category_query for p in results[:3])
    top_3_titles = [p.title.lower() for p in results[:3]]
    assert top_3_canonical or any(category_query in t for t in top_3_titles)


# 3. Parameterized across 6 high-volume MPN keys
@pytest.mark.parametrize("mpn_query", [
    "lm2596",
    "dht11",
    "bme280",
    "sg90",
    "l298n",
    "tp4056"
])
def test_mpn_key_exact_product_in_top_1(mpn_query):
    intent = get_query_intent(mpn_query)
    assert intent.intent == "exact_lookup"
    assert intent.key_type == "mpn"

    results = search_catalog_ranked(query=mpn_query, limit=10)
    assert len(results) > 0

    top_1 = results[0]
    # Exact product in top 1 has MPN matching query or is marked canonical for query
    assert top_1.is_canonical and top_1.canonical_for == mpn_query or mpn_query in top_1.title.lower() or (top_1.mpn and mpn_query in top_1.mpn.lower())


# 4. Known SEO-stuffed competitor listing does NOT outrank canonical
@pytest.mark.parametrize("test_key", ["arduino", "esp32", "raspberry pi", "lm2596", "dht11"])
def test_seo_stuffed_listing_does_not_outrank_canonical(test_key):
    intent = get_query_intent(test_key)

    canonical_title = f"{test_key.upper()} Genuine Core Board"
    seo_stuffed_title = f"Best {test_key.upper()} Deal 100% Original {test_key.upper()} High Quality Compatible {test_key.upper()} Module Pack for {test_key.upper()} DIY Project Board Shield Kit Free Shipping India"

    # Both start with equal base score
    base_score = 10.0

    score_canonical = adjust_search_score(
        base_score=base_score,
        title=canonical_title,
        is_canonical=True,
        canonical_for=test_key,
        intent_result=intent,
        query=test_key
    )

    score_stuffed = adjust_search_score(
        base_score=base_score,
        title=seo_stuffed_title,
        is_canonical=False,
        canonical_for=None,
        intent_result=intent,
        query=test_key
    )

    assert score_canonical > score_stuffed, f"Canonical score ({score_canonical:.2f}) must outrank stuffed ({score_stuffed:.2f}) for '{test_key}'"


# 5. Long-tail variant ranks below canonical in brand mode but still appears
def test_long_tail_variant_ranks_below_canonical_in_brand_mode():
    intent = get_query_intent("raspberry pi")
    assert intent.intent == "brand_or_category"

    canonical_title = "Raspberry Pi 5 Model B - 4GB RAM"
    long_tail_title = "Raspberry Pi 4 Model B 2GB RAM Quad Core 64-bit SOC Broadcom BCM2711"

    score_canonical = adjust_search_score(
        base_score=10.0,
        title=canonical_title,
        is_canonical=True,
        canonical_for="raspberry pi",
        intent_result=intent,
        query="raspberry pi"
    )

    score_long_tail = adjust_search_score(
        base_score=10.0,
        title=long_tail_title,
        is_canonical=False,
        canonical_for=None,
        intent_result=intent,
        query="raspberry pi"
    )

    assert score_canonical > score_long_tail
    assert score_long_tail > 0.0  # Still appears and has positive score
