# Draft classifications for pairs 1-40
pairs_analysis = [
    # 1: gz_47325
    # A: Waveshare 1.3inch LCD Display Module for Raspberry Pi Pico, 65K Colors, 240×240, SPI
    # B: Waveshare 2inch LCD Display Module for Raspberry Pi Pico, 65K Colors, 320×240, SPI - RS2274
    # Both specify screen size and resolution, but values differ (1.3" 240x240 vs 2" 320x240).
    # Same Waveshare Pico display family, different screen size/resolution specs.
    {
        "pair_id": "gz_47325",
        "label": "variant",
        "confidence": "high",
        "reason": "Different screen size (1.3 inch vs 2 inch) and resolution (240x240 vs 320x240) in the Waveshare Pico LCD family."
    },
    # 2: gz_76506
    # A: Infrared IR Sensor Receiver Module
    # B: Infrared IR Sensor Receiver Module for Arduino - RS046
    # Exact same generic IR receiver module, 'for Arduino' is compatibility tagging, RS046 is SKU. Neither has specs the other lacks.
    {
        "pair_id": "gz_76506",
        "label": "same",
        "confidence": "high",
        "reason": "Both are identical generic infrared IR receiver sensor modules with no differing technical specifications."
    },
    # 3: gz_64730
    # A: 5S 15A 18650 Li-ion Lithium Battery BMS Module for 18V-21V Battery
    # B: 5S 15A 18650 Li-ion Lithium Battery BMS Charger Protection Board for 18V 21V Battery
    # Exact same specs: 5S, 15A, 18V-21V, 18650 Li-ion.
    {
        "pair_id": "gz_64730",
        "label": "same",
        "confidence": "high",
        "reason": "Identical 5S 15A 18V-21V 18650 lithium-ion BMS protection board specifications."
    },
    # 4: gz_71206
    # A: SmartElex D4184 Mosfet Control Module
    # B: D4184 MOSFET Control Module for Relay Replacement
    # Both are D4184 MOSFET control modules. SmartElex is Robu's private label for the standard D4184 board; 'for Relay Replacement' is application tag. Same circuit and form factor.
    {
        "pair_id": "gz_71206",
        "label": "same",
        "confidence": "high",
        "reason": "Identical D4184 MOSFET control module circuit and form factor."
    },
    # 5: gz_55175
    # A: 2004 LCD Display RepRapDiscount Smart Controller with Adapter
    # B: 2004 LCD Display Smart Controller with Adapter and Cable
    # Title B specifies 'and Cable' while Title A only specifies 'with Adapter'.
    # Rule: If one title has a spec/bundle component the other lacks -> unsure.
    {
        "pair_id": "gz_55175",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title B explicitly includes ribbon cables while Title A specifies adapter only without indicating cables."
    },
    # 6: gz_54686
    # A: 0.96 inch OLED 0.96 Inch I2C/IIC 4-Pin OLED Display Module - White - RS5444
    # B: GoldenMorning 0.96 Inch I2C/IIC 4-Pin OLED Display Module -WHITE
    # Both are 0.96 inch, I2C/IIC, 4-pin, White OLED display modules. GoldenMorning is the panel OEM, RS5444 is SKU. Drop-in replacement.
    {
        "pair_id": "gz_54686",
        "label": "same",
        "confidence": "high",
        "reason": "Identical 0.96-inch 4-pin I2C white OLED display module specifications and pinout."
    },
    # 7: gz_54697
    # A: 0.96inch 128x64 OLED Display Module
    # B: Waveshare 0.96 Inch OLED Display (B)
    # Title B specifies Waveshare Model (B) (blue, 7-pin SPI/I2C), while Title A omits interface (I2C vs SPI) and color.
    # Rule: If one title has a spec the other lacks -> unsure.
    {
        "pair_id": "gz_54697",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title B specifies Waveshare Model B while Title A lacks interface, pinout, and color specifications."
    },
    # 8: gz_70842
    # A: Solid State Relay Omron G3MB-202P 5VDC In, 240VAC 2A Out
    # B: Solid State Relay G3MB-202P-5VDC 4 Pin 2A 240VAC SSR
    # Both specify Omron G3MB-202P-5VDC, 4-pin SIP, 5VDC input, 240VAC 2A output.
    {
        "pair_id": "gz_70842",
        "label": "same",
        "confidence": "high",
        "reason": "Identical Omron G3MB-202P 5VDC input 240VAC 2A 4-pin solid state relay component."
    },
    # 9: gz_64166
    # A: 14.8V 16.8V BMS 4S High Current up to 30A 18650 Lithium Battery Protection Board
    # B: 4S 30A 18650 Li-ion Lithium Battery Protection BMS Module for 14.8V-16.8V Battery
    # Both specify 4S, 30A, 14.8V-16.8V, 18650 Li-ion battery protection board.
    {
        "pair_id": "gz_64166",
        "label": "same",
        "confidence": "high",
        "reason": "Identical 4S 30A 14.8V-16.8V 18650 lithium-ion BMS protection board specifications."
    },
    # 10: gz_44187
    # A: MEGA TFT LCD Expansion Board TFT LCD Expansion Shield V2.2 for Arduino MEGA - RS3096
    # B: TFT LCD Expansion Shield V2.2 for Arduino MEGA
    # Identical revision V2.2 TFT LCD shield for Arduino MEGA.
    {
        "pair_id": "gz_44187",
        "label": "same",
        "confidence": "high",
        "reason": "Identical TFT LCD expansion shield version 2.2 for Arduino Mega."
    },
    # 11: gz_63520
    # A: 6S 20A 22.2V NMC BMS 18650 Lithium battery protection and charger BMS Module
    # B: 22.2V BMS 6S 20A NMC 18650 Lithium Battery Protection Board
    # Exact match on 6S, 20A, 22.2V, NMC 18650 lithium battery BMS.
    {
        "pair_id": "gz_63520",
        "label": "same",
        "confidence": "high",
        "reason": "Identical 6S 20A 22.2V NMC 18650 lithium battery BMS protection specifications."
    },
    # 12: gz_72358
    # A: 12.6V BMS 3S 40A 18650 Lithium Battery Protection Board
    # B: 3S 40A Li-ion Battery Protection BMS Board for 3.7V NMC cells
    # 3S * 4.2V = 12.6V max (3.7V nominal NMC/18650), both 40A 3S BMS boards.
    {
        "pair_id": "gz_72358",
        "label": "same",
        "confidence": "high",
        "reason": "Identical 3S 40A 12.6V 18650/NMC lithium-ion battery BMS protection board specifications."
    },
    # 13: gz_66448
    # A: Float Sensor Switch For Water Level Controller
    # B: Anti-Corrosion Water Level Sensor with Ball Float Switch
    # Title B specifies anti-corrosion construction and ball float mechanism, which Title A lacks.
    # Rule: If one title has a spec the other lacks -> unsure.
    {
        "pair_id": "gz_66448",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title B specifies anti-corrosion and ball float design specifications that Title A lacks."
    },
    # 14: gz_54734
    # A: Waveshare 0.96inch OLED (A) -RS1702
    # B: Waveshare 0.96 Inch OLED Display (B)
    # Waveshare (A) is yellow/blue dual color, whereas Waveshare (B) is all-blue.
    # Same product family, different color/sub-model spec -> variant.
    {
        "pair_id": "gz_54734",
        "label": "variant",
        "confidence": "high",
        "reason": "Waveshare hardware models (A) and (B) differ in display color configuration (dual yellow-blue vs solid blue)."
    },
    # 15: gz_73161
    # A: Micro SD Card to SD Card Adapter Module
    # B: Micro SD Card Reader Module
    # Distinct parts: passive pin adapter vs active microcontroller SPI reader module.
    # Rule: different: Distinct parts, incompatible form factors.
    {
        "pair_id": "gz_73161",
        "label": "different",
        "confidence": "high",
        "reason": "A passive Micro SD to SD form factor adapter is functionally distinct from an active microcontroller SPI card reader module."
    },
    # 16: gz_56215
    # A: A3967 Stepper Motor Driver For Arduino
    # B: A3967 Stepper Motor Driver Module
    # Identical Allegro A3967 stepper motor driver breakout module.
    {
        "pair_id": "gz_56215",
        "label": "same",
        "confidence": "high",
        "reason": "Identical A3967 stepper motor driver breakout module."
    },
    # 17: gz_55096
    # A: 16x2 (1602) LCD Display (Blue Backlight)
    # B: 16x2 I2C LCD IIC/I2C 1602 LCD Module Blue Backlight - RS403
    # Title B specifies I2C/IIC backpack interface, which Title A lacks (standard parallel 16-pin).
    # Rule: If one title has a spec the other lacks -> unsure.
    {
        "pair_id": "gz_55096",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title B specifies an integrated I2C backpack interface while Title A lacks an interface specification."
    },
    # 18: gz_54703
    # A: 0.96inch 128x64 OLED Display Module
    # B: 128x64 0.96 Inch OLED Display Module I2c Interface - Arduino Compatible
    # Title B specifies I2C interface, while Title A omits interface protocol (could be SPI or I2C).
    # Rule: If one title has a spec the other lacks -> unsure.
    {
        "pair_id": "gz_54703",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title B specifies an I2C communication interface while Title A omits the interface type."
    },
    # 19: gz_62154
    # A: 5V 1 Channel Relay Module
    # B: 5V 10A 1 Channel Relay Module compatible with Arduino
    # Both are standard 5V single-channel relay modules (Songle SRD-05VDC-SL-C rated at 10A). Identical form factor and function.
    {
        "pair_id": "gz_62154",
        "label": "same",
        "confidence": "high",
        "reason": "Both are identical standard 5V single-channel 10A relay modules."
    },
    # 20: gz_77321
    # A: Small Ball Caster Wheel 3 pcs
    # B: Small Caster Wheel
    # Title A is a multi-pack of 3 pcs of ball caster wheel; Title B is an individual caster wheel.
    # Rule: NEVER same-merge bare component with kit, bundle, or pack-of-N.
    # Same component type, different package quantity -> variant.
    {
        "pair_id": "gz_77321",
        "label": "variant",
        "confidence": "high",
        "reason": "Pack-of-3 quantity in Title A differs from the single piece unit in Title B."
    },
    # 21: gz_63842
    # A: 3.7V BMS 1S 1A 18650 Lithium Battery Protection Board
    # B: 1S 3.7V 7.5A 5MOS BMS Li- ion 18650 Battery Protection Board
    # Both are 1S 3.7V 18650 BMS boards, but differing current ratings (1A vs 7.5A 5MOS).
    # Rule: variant: Same component family/chip, different specs: RAM, pin count, channels, voltage, color, material, package.
    {
        "pair_id": "gz_63842",
        "label": "variant",
        "confidence": "high",
        "reason": "Different current discharge ratings (1A vs 7.5A 5MOS) within the 1S 3.7V BMS family."
    },
    # 22: gz_56924
    # A: Waveshare 1.3inch LCD Display Module for Raspberry Pi Pico, 65K Colors, 240×240, SPI
    # B: Waveshare 2inch LCD Display Module for Raspberry Pi Pico, 65K Colors, 320×240, SPI - RS2274
    # Screen size 1.3" 240x240 vs 2" 320x240.
    {
        "pair_id": "gz_56924",
        "label": "variant",
        "confidence": "high",
        "reason": "Different screen size (1.3 inch vs 2 inch) and resolution (240x240 vs 320x240) in the Waveshare Pico LCD family."
    },
    # 23: gz_63063
    # A: 5V Dual Channel Relay Module with Optocoupler
    # B: 4-Channel 12V Relay Module with Optocoupler
    # Channel count (2 vs 4) and coil voltage (5V vs 12V) both differ.
    {
        "pair_id": "gz_63063",
        "label": "variant",
        "confidence": "high",
        "reason": "Different channel count (2-channel vs 4-channel) and coil operating voltage (5V vs 12V)."
    },
    # 24: gz_62911
    # A: 5V 2 Channel Solid State Relay Module 240V 2A Output with Resistive Fuse
    # B: 5V 1 Channel SSR G3MB-202P Solid State Relay Module 240V 2A Output with Resistive Fuse
    # Channel count differs: 2 Channel vs 1 Channel SSR.
    {
        "pair_id": "gz_62911",
        "label": "variant",
        "confidence": "high",
        "reason": "Different channel count (2-channel vs 1-channel) in 5V 240V 2A solid state relay module."
    },
    # 25: gz_72067
    # A: 3.7V BMS 1S 2A 18650 Lithium Battery Protection Board
    # B: 1S 18650 Li-ion Lithium Battery BMS Charger Protection Board for 3.7V Battery.
    # Title A specifies 2A current rating, while Title B omits the current rating entirely.
    # Rule: If one title has a spec the other lacks -> unsure.
    {
        "pair_id": "gz_72067",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title A specifies a 2A current limit while Title B omits the current rating."
    },
    # 26: gz_63522
    # A: 14.8V 16.8V BMS 4S High Current up to 30A 18650 Lithium Battery Protection Board
    # B: 4S 30A 18650 Li-ion Lithium Battery Protection BMS Module for 14.8V-16.8V Battery
    # Identical 4S 30A 14.8V-16.8V 18650 Li-ion BMS.
    {
        "pair_id": "gz_63522",
        "label": "same",
        "confidence": "high",
        "reason": "Identical 4S 30A 14.8V-16.8V 18650 lithium-ion BMS protection board specifications."
    },
    # 27: gz_76500
    # A: Infrared IR Sensor Receiver Module
    # B: TSOP 1838 Infrared IR Sensor Receiver Module
    # Title B specifies the exact IC model TSOP1838, while Title A lacks the IC specification.
    # Rule: If one title has a spec the other lacks -> unsure.
    {
        "pair_id": "gz_76500",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title B specifies the TSOP1838 receiver IC while Title A lacks an IC specification."
    },
    # 28: gz_62334
    # A: 6 Channel Relay Module 5V High and Low Level Trigger Relay Control
    # B: 2 Channel Relay Module 5V 40A/30A High and Low Level Trigger Relay Control Module
    # Channel count (6-channel vs 2-channel) and current rating differ.
    {
        "pair_id": "gz_62334",
        "label": "variant",
        "confidence": "high",
        "reason": "Different channel count (6-channel vs 2-channel) and relay current capacity."
    },
    # 29: gz_77085
    # A: Analog Sound Sensor Microphone Module for Arduino
    # B: Keyestudio Analog Sound Noise Sensor Detection Module for Arduino
    # Keyestudio is a proprietary module manufacturer with unique PCB pinout/form-factor, whereas A is generic sound sensor.
    # Rule: If one title has a spec/brand the other lacks -> unsure.
    {
        "pair_id": "gz_77085",
        "label": "unsure",
        "confidence": "high",
        "reason": "Title B specifies Keyestudio proprietary module while Title A is an unbranded generic sound sensor."
    },
    # 30: gz_54700
    # A: 0.96inch 128x64 OLED Display Module
    # B: 0.96 inch OLED display rotary encoder combination module
    # Title B is a combination module integrating a rotary encoder on-board with the OLED.
    # Distinct parts / form factor -> different.
    {
        "pair_id": "gz_54700",
        "label": "different",
        "confidence": "high",
        "reason": "Title B is an integrated combination module with a rotary encoder, distinct from a standalone OLED display."
    },
    # 31: gz_66186
    # A: Rain Drop Sensor Module
    # B: Rain Drop Detection Sensor Module
    # Both are the standard 2-piece rain drop sensor module (conductive PCB board + LM393 comparator module).
    {
        "pair_id": "gz_66186",
        "label": "same",
        "confidence": "high",
        "reason": "Identical rain drop detection sensor module and comparator board."
    },
    # 32: gz_59113
    # A: TFT LCD Expansion Shield V2.2 for Arduino MEGA
    # B: MEGA TFT LCD Expansion Board TFT LCD Expansion Shield V2.2 for Arduino MEGA - RS3096
    # Exact match on V2.2 TFT LCD Shield for Arduino Mega.
    {
        "pair_id": "gz_59113",
        "label": "same",
        "confidence": "high",
        "reason": "Identical TFT LCD expansion shield version 2.2 for Arduino Mega."
    },
    # 33: gz_72087
    # A: 3.7V BMS 1S 1A 18650 Lithium Battery Protection Board
    # B: 1S 3.7V 7.5A 5MOS BMS Li- ion 18650 Battery Protection Board
    # Different current ratings: 1A vs 7.5A (5MOS).
    {
        "pair_id": "gz_72087",
        "label": "variant",
        "confidence": "high",
        "reason": "Different current discharge ratings (1A vs 7.5A 5MOS) within the 1S 3.7V BMS family."
    },
    # 34: gz_72060
    # A: 3.7V BMS 1S 2A 18650 Lithium Battery Protection Board
    # B: 1S 3.7V 3A 2MOS BMS Li-ion 18650 Battery Protection Board
    # Different current ratings: 2A vs 3A (2MOS).
    {
        "pair_id": "gz_72060",
        "label": "variant",
        "confidence": "high",
        "reason": "Different current ratings (2A vs 3A 2MOS) within the 1S 3.7V BMS family."
    },
    # 35: gz_62597
    # A: Solid State Relay Omron G3MB-202P 5VDC In, 240VAC 2A Out
    # B: Solid State Relay G3MB-202P-5VDC 4 Pin 2A 240VAC SSR
    # Both specify Omron G3MB-202P-5VDC 4-pin 2A 240VAC SSR.
    {
        "pair_id": "gz_62597",
        "label": "same",
        "confidence": "high",
        "reason": "Identical Omron G3MB-202P 5VDC input 240VAC 2A solid state relay component."
    },
    # 36: gz_74185
    # A: Capacitive Soil Moisture Sensor V1.2
    # B: Capacitive Soil Moisture Sensor V2.0
    # Hardware revision difference: V1.2 vs V2.0 (different internal circuitry / corrosion resistance / analog output range).
    # Same sensor family, different hardware revision -> variant.
    {
        "pair_id": "gz_74185",
        "label": "variant",
        "confidence": "high",
        "reason": "Different hardware revisions (V1.2 vs V2.0) with modified circuit and output characteristics."
    },
    # 37: gz_54864
    # A: 0.96\" OLED Display Module - SPI/I2C - 128x64 - 7 Pin (Blue)
    # B: 0.96\" I2C IIC SPI Serial 128X64 Blue OLED LCD LED Display Module for Arduino-RS1189
    # Both: 0.96\", SPI/I2C serial, 128x64, Blue OLED display module.
    {
        "pair_id": "gz_54864",
        "label": "same",
        "confidence": "high",
        "reason": "Identical 0.96-inch 128x64 blue OLED display module with configurable SPI/I2C interface."
    },
    # 38: gz_63191
    # A: 1 Channel 12V 10A Relay Module with Optocoupler
    # B: 1 channel 5V 10A Relay Control Board Module with Optocoupler
    # Coil operating voltage differs: 12V vs 5V.
    {
        "pair_id": "gz_63191",
        "label": "variant",
        "confidence": "high",
        "reason": "Different coil trigger operating voltages (12V vs 5V) within the single-channel optocoupler relay module family."
    },
    # 39: gz_74985
    # A: DYP-A08BNYTB-V1.0 Waterproof Ultrasonic Sensor Module 25–500cm Distance & Level Measurement Module
    # B: DYP-A08BNYTB-V1.0 UART controlled  Waterproof Ultrasonic Level Sensor (Measuring Range 25~500cm)
    # Exact same manufacturer MPN: DYP-A08BNYTB-V1.0 (waterproof ultrasonic sensor, UART controlled, 25-500cm).
    {
        "pair_id": "gz_74985",
        "label": "same",
        "confidence": "high",
        "reason": "Identical DYP-A08BNYTB-V1.0 waterproof ultrasonic distance and level sensor MPN and range."
    },
    # 40: gz_72088
    # A: 3.7V BMS 1S 1A 18650 Lithium Battery Protection Board
    # B: 1S 3.7V 4.5A 3MOS BMS Li-ion 18650 Battery Protection Board
    # Different current ratings: 1A vs 4.5A (3MOS).
    {
        "pair_id": "gz_72088",
        "label": "variant",
        "confidence": "high",
        "reason": "Different current ratings (1A vs 4.5A 3MOS) within the 1S 3.7V BMS family."
    }
]
