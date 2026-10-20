"""Role/attachment ranking regressions (shared by web search and MCP)."""
import pytest

from models import UnifiedProduct
from normalizer import ComponentItem, can_merge, classify_product_form
from src.search.product_prior import coverage, query_affinity, query_category_hint
from src.search.retrieval import rank_products


def _p(index: int, title: str, mpn: str = "X") -> UnifiedProduct:
    return UnifiedProduct(
        vendor="robu",
        vendor_name="Robu.in",
        external_id=f"e{index}",
        title=title,
        normalized_title=title.lower(),
        mpn=mpn,
        price=100.0 + index,
        available=True,
        product_url=f"https://example.test/{index}",
    )


def test_accessory_title_does_not_outrank_the_part():
    """BM25 can put the short accessory first; the shared ranker must not."""
    products = [
        _p(0, "Dc Motor Mounting Bracket For IG52", "IG52"),
        _p(1, "Replaceable Carbon Brush for IG52 Motor (MOQ 2PCS)", "IG52"),
        _p(2, "24V 60RPM 100W IG52 Extra Heavy Duty Planetary Geared DC motor 160kgcm", "IG52"),
    ]
    ranked = rank_products(products, "IG52")
    assert "motor" in ranked[0].title.lower()
    assert "bracket" not in ranked[0].title.lower()


def test_accessory_query_still_ranks_accessories_first():
    products = [
        _p(0, "24V 60RPM 100W IG52 Extra Heavy Duty Planetary Geared DC motor", "IG52"),
        _p(1, "Dc Motor Mounting Bracket For IG52", "BRACKET"),
    ]
    ranked = rank_products(products, "motor bracket")
    assert "bracket" in ranked[0].title.lower()


def test_kit_does_not_outrank_an_individual_component():
    products = [
        _p(0, "Resistor Combo Kit (30 values, 5 each - 150 resistors)", "KIT"),
        _p(1, "330R R0805 SMD Resistor", "RES"),
    ]
    ranked = rank_products(products, "resistor")
    assert "resistor" in ranked[0].title.lower()
    assert "kit" not in ranked[0].title.lower()


def test_secondary_feature_does_not_define_the_product():
    products = [
        _p(0, "Digital PWM Generator with Display", "PWM"),
        _p(1, "0.96inch 128x64 OLED Display Module", "OLED"),
    ]
    ranked = rank_products(products, "display")
    assert "oled" in ranked[0].title.lower()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("display", "displays"),
        ("led", "leds"),
        ("battery", "battery"),
        ("resistor", "resistors"),
        ("raspberry pi 5", "modules"),
        ("nema17 stepper", "motor"),
        ("IG52", None),
        ("battery holder", None),
        ("audio amplifier", None),
    ],
)
def test_query_category_hint(query, expected):
    assert query_category_hint(query) == expected


def test_category_hint_prefers_the_taxonomy_category():
    """A 3D-printer display kit must not outrank a real display for 'display'."""
    oled = _p(0, "0.96inch 128x64 OLED Display Module", "OLED")
    oled.category_canonical = "displays"
    kit = _p(1, "Creality Display Kits", "KIT")
    kit.category_canonical = "printing"

    ranked = rank_products([kit, oled], "display")
    assert ranked[0].category_canonical == "displays"


def test_plural_title_variants_merge_into_one_card():
    a = ComponentItem("Creality Display Kits", 100.0)
    b = ComponentItem("Creality Display Kit", 100.0)
    assert classify_product_form(a.title) == classify_product_form(b.title) == "kit"
    merged, reason = can_merge(a, b)
    assert merged, reason


def test_accessory_prior_does_not_fire_when_query_asks_for_it():
    assert query_affinity("2xAA Battery Holder", "battery holder") > 0
    assert query_affinity("2xAA Battery Holder", "battery") < query_affinity(
        "18650 Lithium Battery Cell", "battery"
    )


def test_prefix_family_query_counts_as_coverage():
    assert coverage("stm32", "STM32F103C8T6 Development Board") == 1.0
    assert coverage("esp32", "ESP32-S3-WROOM-1 Development Board") == 1.0


def test_raspberry_pi_accessory_does_not_outrank_the_board():
    products = [
        _p(0, "Official Raspberry Pi 5 Active Cooler", "COOLER"),
        _p(1, "Raspberry Pi 5 Model 8GB RAM", "RPI5"),
    ]
    ranked = rank_products(products, "Raspberry Pi 5")
    assert "model" in ranked[0].title.lower() or "board" in ranked[0].title.lower()


def test_kit_group_stays_below_an_individual_component():
    """Group ranking must not reward a combo kit for a bare component query."""
    from search_service import AggregatorEngine

    kit = _p(0, "Resistor Combo Kit (30 values, 5 each - 150 resistors)", "KIT")
    single = _p(1, "330R R0805 SMD Resistor", "RES")
    groups = AggregatorEngine().group_and_compare([kit, single], query="resistor")
    assert groups[0].canonical_name == single.title


def test_in_stock_card_outranks_out_of_stock_alternative():
    from search_service import AggregatorEngine

    missing = _p(0, "Test Sensor Alpha", "SENSOR-A")
    missing.available = False
    in_stock = _p(1, "Test Sensor Beta", "SENSOR-B")
    in_stock.available = True

    groups = AggregatorEngine().group_and_compare([missing, in_stock], query="sensor")
    assert groups[0].canonical_name == in_stock.title


def test_multi_vendor_card_outranks_single_vendor_for_broad_query():
    from search_service import AggregatorEngine

    single = _p(0, "Test Widget One", "WIDGET-1")
    multi_a = _p(1, "Test Widget Two", "WIDGET-2")
    multi_b = _p(2, "Test Widget Two", "WIDGET-2")
    multi_b.vendor = "quartzcomponents"
    multi_b.vendor_name = "Quartz Components"
    multi_b.external_id = "e2-quartz"

    groups = AggregatorEngine().group_and_compare(
        [single, multi_a, multi_b], query="widget"
    )
    assert groups[0].canonical_name == "Test Widget Two"
    assert groups[0].total_offers == 2


def test_exact_mpn_keeps_the_part_ahead_of_stock_alternative():
    from search_service import AggregatorEngine

    exact = _p(0, "Widget X Reference Board", "WIDGET-X")
    exact.available = False
    alternative = _p(1, "Widget Y Board", "WIDGET-Y")
    alternative.available = True

    groups = AggregatorEngine().group_and_compare([exact, alternative], query="WIDGET-X")
    assert groups[0].mpn == "WIDGET-X"


@pytest.mark.parametrize(
    "model_query,expected_token",
    [
        ("IG52", "motor"),
        ("NEMA17 stepper", "motor"),
        ("Raspberry Pi 5", "model"),
        ("CH340", "converter"),
    ],
)
def test_web_and_mcp_share_the_same_top_result(model_query, expected_token):
    """The MCP text path uses the same ranker as the web listing path."""
    import mcp_server.server as server
    from src.search.retrieval import search_catalog_ranked

    web = search_catalog_ranked(model_query, limit=5)
    mcp = server.search_components(query=model_query, limit=5, refresh=False)
    assert web, f"web search returned nothing for {model_query!r}"
    assert mcp.ok and (mcp.data or {}).get("results")
    assert expected_token in web[0].title.lower(), web[0].title
    assert expected_token in (mcp.data or {})["results"][0]["title"].lower()
