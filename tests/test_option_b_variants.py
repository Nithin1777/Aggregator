"""
Unit tests for Task 2: Option B Conservative Merge & Variant Families.
Verifies:
- can_merge() exact behavior and spec signature gating
- variant_families grouping for same MPN with differing specs
- Best price badge computed across same-spec offers only
- Every output row carries merge_reason and variant_key
"""
import pytest
from models import UnifiedProduct
from normalizer import ComponentItem, can_merge, can_merge_three_outcome, extract_spec_signature
from search_service import AggregatorEngine

def test_can_merge_delegates_to_v2():
    # Known V2-mergeable pair: DHT11 sensor modules
    a = ComponentItem("DHT11 Temperature and Humidity Sensor Module", mpn="DHT11")
    b = ComponentItem("DHT11 Digital Humidity & Temperature Sensor Module", mpn="DHT11")
    merged, reason = can_merge(a, b)
    assert merged is True
    assert reason.startswith("v2_matcher:")

def test_can_merge_exact_spec_match():
    # Matching 5V 2-channel relay modules tested against preserved 3-outcome semantics
    a = ComponentItem("5V 2 Channel Relay Module with Optocoupler", mpn="RELAY-5V")
    b = ComponentItem("Dual Channel 5V Relay Board for Arduino", mpn="RELAY-5V")
    assert a.spec_sig == b.spec_sig
    merged, reason = can_merge_three_outcome(a, b)
    assert merged is True
    assert reason == "spec_exact"

def test_can_merge_differing_channels_veto():
    # Differing channel count (2-channel vs 4-channel)
    a = ComponentItem("5V 2 Channel Relay Module", mpn="RELAY-5V")
    b = ComponentItem("5V 4 Channel Relay Module", mpn="RELAY-5V")
    assert a.spec_sig != b.spec_sig
    merged, reason = can_merge_three_outcome(a, b)
    assert merged is False
    assert reason == "spec_variant"

def test_can_merge_differing_current_veto():
    # Differing current rating (10A vs 30A)
    a = ComponentItem("5V 1 Channel 10A Relay Module", mpn="RELAY-5V")
    b = ComponentItem("5V 1 Channel 30A Relay Module", mpn="RELAY-5V")
    assert a.spec_sig != b.spec_sig
    merged, reason = can_merge_three_outcome(a, b)
    assert merged is False
    assert reason == "spec_variant"

def test_can_merge_missing_required_spec_veto():
    # Missing required spec (voltage not specified on relay -> spec_sig is empty)
    a = ComponentItem("Relay Module", mpn="RELAY")
    b = ComponentItem("5V 1 Channel Relay Module", mpn="RELAY")
    assert a.spec_sig == ""
    merged, reason = can_merge_three_outcome(a, b)
    assert merged is False
    assert "deferred_tier_b" in reason

def test_can_merge_form_mismatch_veto():
    # Bare IC (chip) vs assembled breakout module
    a = ComponentItem("DS1307 Real Time Clock IC SOIC-8", mpn="DS1307", form="chip")
    b = ComponentItem("DS1307 RTC Module for Arduino", mpn="DS1307", form="module")
    merged, reason = can_merge_three_outcome(a, b)
    assert merged is False
    assert "deferred_tier_b" in reason
    assert "Product form mismatch" in reason

def test_variant_families_and_same_spec_best_price(monkeypatch):
    # Tests preserved 3-outcome variant families grouping semantics using monkeypatch
    monkeypatch.setattr("search_service.can_merge", can_merge_three_outcome)
    engine = AggregatorEngine()

    products = [
        # Two offers for 2-channel relay (Vendor A cheaper)
        UnifiedProduct(
            vendor="quartzcomponents",
            vendor_name="Quartz",
            external_id="q1",
            title="5V 2 Channel Relay Module",
            normalized_title="5v 2 channel relay module",
            mpn="RELAY-5V",
            price=60.0,
            available=True,
            product_url="https://quartz.com/q1"
        ),
        UnifiedProduct(
            vendor="robu",
            vendor_name="Robu",
            external_id="r1",
            title="Dual Channel 5V Relay Module Board",
            normalized_title="dual channel 5v relay module board",
            mpn="RELAY-5V",
            price=75.0,
            available=True,
            product_url="https://robu.in/r1"
        ),
        # One offer for 4-channel relay under the same family
        UnifiedProduct(
            vendor="makerbazar",
            vendor_name="MakerBazar",
            external_id="m1",
            title="5V 4 Channel Relay Module Board",
            normalized_title="5v 4 channel relay module board",
            mpn="RELAY-5V",
            price=120.0,
            available=True,
            product_url="https://makerbazar.in/m1"
        )
    ]

    groups = engine.group_and_compare(products)

    # Must produce 2 separate ComparisonGroups (one for 2ch, one for 4ch)
    assert len(groups) == 2

    group_2ch = next(g for g in groups if "2ch" in g.variant_key)
    group_4ch = next(g for g in groups if "4ch" in g.variant_key)

    # 1. Best Price badge uses same-spec offers only
    assert group_2ch.best_price == 60.0
    assert group_2ch.total_offers == 2
    assert group_4ch.best_price == 120.0
    assert group_4ch.total_offers == 1

    # 2. Variants shown as sub-list under the card
    assert len(group_2ch.variants) == 1
    assert group_2ch.variants[0]["variant_key"] == group_4ch.variant_key
    assert group_2ch.variants[0]["from_price"] == 120.0

    assert len(group_4ch.variants) == 1
    assert group_4ch.variants[0]["variant_key"] == group_2ch.variant_key
    assert group_4ch.variants[0]["from_price"] == 60.0

    # 3. Every output row carries merge_reason and variant_key
    assert group_2ch.merge_reason == "spec_exact"
    assert group_2ch.variant_key is not None
    for offer in group_2ch.offers:
        assert offer.merge_reason == "spec_exact"
        assert offer.variant_key == group_2ch.variant_key

    assert group_4ch.merge_reason == "single_offer"
    assert group_4ch.variant_key is not None
    for offer in group_4ch.offers:
        assert offer.merge_reason == "single_offer"
        assert offer.variant_key == group_4ch.variant_key
