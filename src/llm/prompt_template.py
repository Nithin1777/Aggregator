"""
Single source of truth for LLM classification prompt rubric and calibration examples.
Imported by resolver.py to ensure the prompt hash is stable and identical.
"""
import hashlib

RUBRIC_BLOCK = """## Decision Rubric

### 1. SAME
The two listings represent the exact same functional electronic component or module that can serve as a direct drop-in replacement.
- Same core MPN, chip model, or industry-standard circuit module.
- Identical electrical specifications (voltage, current, channels, memory capacity, logic levels).
- Compatible package/footprint and pinout.
- Minor branding differences (vendor white-label rebrands such as SmartElex vs generic), minor vendor SKU suffixes, or application tags (such as "for Arduino") do NOT make products different if the underlying hardware is identical.

### 2. DIFFERENT
The two listings represent different, incompatible products or variants that cannot serve as drop-in replacements.
- Different core chip, MPN, or architecture (e.g., ESP32 vs ESP32-S3 vs ESP32-C3; Raspberry Pi Pico vs Pico W).
- Conflicting electrical or hardware specifications: differing RAM capacity (4GB vs 8GB vs 16GB), channel count (2-channel vs 4-channel), coil/operating voltage (5V vs 12V), current rating (1A vs 7.5A), display size/resolution (1.3" vs 2.0").
- Incompatible form factor, package type, or physical interface (e.g., bare IC vs assembled breakout board; adapter plate vs active converter; 30-pin vs 38-pin).
- Bundles, packs, or kits vs individual components (e.g., 3 pcs pack vs single item; board + cables vs bare board).

### 3. UNSURE
Insufficient information to make an unambiguous determination.
- One listing specifies a critical hardware specification (interface type like I2C vs SPI, voltage, or specific IC model like TSOP1838) while the other listing is completely silent or ambiguous on that specification.
- Insufficient or contradictory details in the listing title preventing certainty.

### Strict Hardware Rules
- NEVER merge ESP32 base with subfamilies (S2, S3, C3, C6) or Pico base with Pico W.
- NEVER merge across RAM capacities (4GB vs 8GB vs 16GB) or pin counts (30-pin vs 38-pin).
- NEVER merge different USB bridge ICs (CH340 vs CP2102 vs FT232).
- NEVER merge a bare IC/chip with an assembled breakout module or PCB.
- NEVER merge a development board with an expansion baseboard/shield or adapter plate.
- NEVER merge a bare component with a starter kit / bundle / pack-of-N.
- If one title specifies a critical spec and the other is completely generic or silent, do NOT merge (mark UNSURE)."""

CALIBRATION_EXAMPLES_BLOCK = """## Calibration examples (from the human-labeled seed)

### Example 1 (SAME):
Input:
{"pair_id": "gz_64730", "title_a": "5S 15A 18650 Li-ion Lithium Battery BMS Module for 18V-21V Battery", "specs_a": {}, "title_b": "5S 15A 18650 Li-ion Lithium Battery BMS Charger Protection Board for 18V 21V Battery", "specs_b": {}}
Output:
{"pair_id": "gz_64730", "label": "same", "confidence": "high", "deciding_spec": "5S 15A BMS for 18V-21V battery", "reason": "Both listings specify the same 5S 15A lithium battery protection board for the same voltage range."}

### Example 2 (DIFFERENT):
Input:
{"pair_id": "gz_47325", "title_a": "Waveshare 1.3inch LCD Display Module for Raspberry Pi Pico, 65K Colors, 240×240, SPI", "specs_a": {"board_model": "pico_base", "screen_size": "1.3"}, "title_b": "Waveshare 2inch LCD Display Module for Raspberry Pi Pico, 65K Colors, 320×240, SPI - RS2274", "specs_b": {"board_model": "pico_base", "screen_size": "2"}}
Output:
{"pair_id": "gz_47325", "label": "different", "confidence": "high", "deciding_spec": "screen size/resolution (1.3in 240x240 vs 2in 320x240)", "reason": "Different display size and resolution change the mechanical footprint and framebuffer requirements."}

### Example 3 (UNSURE):
Input:
{"pair_id": "gz_66448", "title_a": "Float Sensor Switch For Water Level Controller", "specs_a": {}, "title_b": "Anti-Corrosion Water Level Sensor with Ball Float Switch", "specs_b": {}}
Output:
{"pair_id": "gz_66448", "label": "unsure", "confidence": "high", "deciding_spec": "anti-corrosion material and ball float mechanism missing in Title A", "reason": "Title B specifies anti-corrosion and ball float design specifications that Title A lacks."}

### Example 4 (TRICKY BUT CLEAR - SAME):
Input:
{"pair_id": "gz_71206", "title_a": "SmartElex D4184 Mosfet Control Module", "specs_a": {}, "title_b": "D4184 MOSFET Control Module for Relay Replacement", "specs_b": {}}
Output:
{"pair_id": "gz_71206", "label": "same", "confidence": "high", "deciding_spec": "D4184 MOSFET control module", "reason": "SmartElex D4184 is a vendor white-label rebrand of the generic D4184 MOSFET control module; application tagging does not alter the hardware."}"""


def get_rubric_hash() -> str:
    """Returns SHA256 of the normalized rubric block."""
    return hashlib.sha256(RUBRIC_BLOCK.strip().encode("utf-8")).hexdigest()


def get_prompt_text(input_file: str = "data/not_sure.jsonl", output_file: str = "data/not_sure_classified.jsonl") -> str:
    """Generates the full self-contained markdown instructions for the external harness."""
    return f"""# Classification task

You will classify pairs of electronics product listings from Indian vendors (Robu, QuartzComponents, MakerBazar, Robokits, REES52) as SAME, DIFFERENT, or UNSURE. Evaluate on functional drop-in substitutability.

{RUBRIC_BLOCK}

## Input

Read {input_file}. Each line is:
  {{"pair_id": "<id>", "title_a": "...", "specs_a": {{...}}, "title_b": "...", "specs_b": {{...}}}}
specs_a/specs_b may be object, string, or empty.

## Output

Write {output_file}. Exactly one JSON line per input pair, same order, same count. Schema:

  {{"pair_id": "<id>", "label": "same|different|unsure", "confidence": "high|medium|low", "deciding_spec": "<the trait that decided it>", "reason": "<one sentence>"}}

No preamble. No markdown fences. No commentary. Do not skip lines. Do not merge lines.

{CALIBRATION_EXAMPLES_BLOCK}
"""
