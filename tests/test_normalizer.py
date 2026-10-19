"""
Unit tests for component title normalizer and MPN extraction.
"""
from normalizer import (
    calculate_token_similarity,
    extract_mpn,
    canonical_base_mpn,
    match_components_v2,
    get_canonical_cluster_key,
    normalize_title,
)

def test_extract_mpn_esp32_variants():
    assert extract_mpn("ESP32-CAM WiFi + Bluetooth Camera Module OV2640") == "ESP32-CAM"
    assert extract_mpn("NodeMCU-32S ESP32 Development Board") == "NODEMCU-32S"
    assert extract_mpn("ESP32-WROOM-32D Dual-Core WiFi BLE Module") == "ESP32-WROOM-32D"
    assert extract_mpn("ESP32 SuperMini Development Board") == "ESP32"

def test_inverted_esp32_subfamily_defaults():
    # Known equivalent collapses to base
    assert canonical_base_mpn("ESP32") == "ESP32"
    assert canonical_base_mpn("ESP32-DEVKITC") == "ESP32"
    
    # Specific subfamilies stay specific
    assert canonical_base_mpn("ESP32-C3") == "ESP32-C3"
    assert canonical_base_mpn("ESP32-S3") == "ESP32-S3"
    assert canonical_base_mpn("ESP32-H2") == "ESP32-H2"
    assert canonical_base_mpn("ESP32-C61") == "ESP32-C61"
    
    # Future unreleased or unknown subfamilies must NOT collapse to base ESP32 (deny by default)
    assert canonical_base_mpn("ESP32-P4") == "ESP32-P4"
    assert canonical_base_mpn("ESP32-C5") == "ESP32-C5"
    assert canonical_base_mpn("ESP32-H4") == "ESP32-H4"
    assert canonical_base_mpn("ESP32-NEWCHIP") == "ESP32-NEWCHIP"
    
    # Matching check: Future ESP32-P4 must NEVER merge with base ESP32
    is_m, _, reason = match_components_v2(
        "Espressif ESP32-P4 High-Performance RISC-V Board",
        "Espressif ESP32 Standard Dev Board",
        450.0, 450.0
    )
    assert not is_m
    assert "Tier A.5 MPN conflict" in reason

def test_extract_mpn_sensors_and_ics():
    assert extract_mpn("MPU6050 6-DOF 3-Axis Gyroscope and Accelerometer") == "MPU-6050"
    assert extract_mpn("BME280 Digital Pressure Temperature Humidity Sensor") == "BME280"
    assert extract_mpn("LM2596 Step-Down DC-DC Buck Converter 3A") == "LM2596"
    assert extract_mpn("STM32F401CCU6 Minimum System Board Microcomputer") == "STM32F401CCU6"
    assert extract_mpn("Genuine NE555 Precision Timer DIP-8 IC") == "NE555"

def test_normalize_title():
    raw = "Original High Quality ESP32-CAM WiFi Module (Pack of 1) with Fast Delivery"
    norm = normalize_title(raw)
    assert "original" not in norm
    assert "pack" not in norm
    assert "quality" not in norm
    assert "esp32-cam" in norm

def test_token_similarity():
    t1 = "esp32 development board wifi bluetooth module"
    t2 = "esp32 wifi bluetooth dev board module type-c"
    sim = calculate_token_similarity(t1, t2)
    assert sim > 0.5

def test_cluster_key():
    k1 = get_canonical_cluster_key("ESP32-CAM Development Board with OV2640")
    k2 = get_canonical_cluster_key("Robu ESP32-CAM Camera Module")
    assert k1 == k2 == "mpn:ESP32-CAM"
