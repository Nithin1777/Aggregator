"""
Pytest integration for matching accuracy, regression detection, and adversarial veto verification.
"""
import pytest
from eval_matcher import baseline_matcher, evaluate_matcher
from normalizer import match_components_v2

# Named critical adversarial pairs that must NEVER merge under any circumstances
NEVER_MERGE_ADVERSARIAL_PAIRS = [
    (
        "CH340 USB to TTL UART Serial Converter Adapter Module",
        "CP2102 USB to TTL UART Serial Converter Adapter Module",
        "CH340 vs CP2102 distinct USB-UART bridges"
    ),
    (
        "LM2596 DC-DC Step Down Converter Power Supply Module",
        "AMS1117 Step Down Voltage Regulator Power Supply Module",
        "LM2596 switching regulator vs AMS1117 linear LDO"
    ),
    (
        "ESP32-WROOM-32 30 Pin Development Board",
        "ESP32-WROVER 30 Pin Development Board with PSRAM",
        "ESP32 WROOM vs WROVER (PSRAM variance)"
    ),
    (
        "Raspberry Pi 4 Model B - 4GB RAM",
        "Raspberry Pi 4 Model B - 8GB RAM",
        "Raspberry Pi 4GB vs 8GB RAM capacity"
    ),
    (
        "ESP32 NodeMCU WiFi Development Board 30 Pins",
        "ESP32 NodeMCU WiFi Development Board 38 Pins",
        "30-Pin vs 38-Pin breadboard pinout mismatch"
    ),
    (
        "3.3V to 5V 4-Channel Logic Level Converter Shifter Module",
        "5V to 3.3V 4-Channel Step Down Power Converter Module",
        "Directional inversion (level shifter vs step down)"
    ),
    (
        "Raspberry Pi Pico RP2040 Microcontroller Board",
        "Raspberry Pi Pico W RP2040 Microcontroller Board with WiFi",
        "Pico base (no wireless) vs Pico W (WiFi)"
    ),
]


def test_baseline_matcher_runs():
    """Validates that the baseline matcher evaluates cleanly on golden dataset."""
    results = evaluate_matcher(baseline_matcher, matcher_name="Baseline Test", split="dev")
    assert results["total"] > 0
    assert "precision" in results
    assert "recall" in results


def test_matcher_v2_dev_and_held_out_accuracy():
    """
    Validates Matcher V2 achieves high precision on dev, and tracks held_out active baseline
    now containing newly observed adversarial failure cases.
    """
    dev_results = evaluate_matcher(match_components_v2, matcher_name="Matcher V2 (dev)", split="dev")
    assert dev_results["precision"] >= 0.95, f"[dev] Precision fell below 0.95: {dev_results['precision']}"
    assert dev_results["recall"] >= 0.90, f"[dev] Recall fell below 0.90: {dev_results['recall']}"

    held_results = evaluate_matcher(match_components_v2, matcher_name="Matcher V2 (held_out)", split="held_out")
    # Held out now actively contains 5 real-world adversarial failures discovered in wild audit
    assert held_results["recall"] >= 0.90, f"[held_out] Recall fell below 0.90: {held_results['recall']}"
    assert held_results["precision"] >= 0.50, f"[held_out] Precision fell below baseline 0.50: {held_results['precision']}"

    # Hard MPN conflicts must have 100% precision (0 false merges allowed)
    if "hard_negative_mpn_conflict" in dev_results["categories"]:
        assert dev_results["categories"]["hard_negative_mpn_conflict"]["precision"] >= 1.0



@pytest.mark.parametrize("title_a,title_b,description", NEVER_MERGE_ADVERSARIAL_PAIRS)
def test_adversarial_never_merge_pairs(title_a: str, title_b: str, description: str):
    """
    Guarantees strict 0 False Positives on named adversarial hardware conflict pairs.
    These pairs must never be merged by Matcher V2.
    """
    is_match, score, reason = match_components_v2(title_a, title_b)
    assert not is_match, f"Adversarial pair merged: {description}! Reason: {reason} (Score: {score})"
