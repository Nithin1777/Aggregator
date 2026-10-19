import re

def normalize_scalar(key: str, val: str, v_type: str = "string", unit: str = None):
    s = str(val).strip()
    # Cells
    if key == "cells" or (isinstance(s, str) and re.search(r"^\d+\s*s(\s+lipo|\s+li-ion)?$", s, re.I)):
        m = re.search(r"^(\d+)\s*s", s, re.I)
        if m:
            return f"{m.group(1)}s".lower()

    # Torque (e.g. 2.5kg-cm / 2.5 kgcm)
    if "torque" in key or (isinstance(s, str) and re.search(r"kg[\s-]*cm", s, re.I)):
        m_t = re.search(r"^([\d\.]+)\s*kg[\s-]*cm$", s, re.I)
        if m_t:
            return float(m_t.group(1))

    # Capacity (mAh)
    if key == "capacity_mah" or unit == "mAh":
        # Check Ah
        m_ah = re.search(r"^([\d\.]+)\s*ah$", s, re.I)
        if m_ah:
            return int(round(float(m_ah.group(1)) * 1000))
        m_mah = re.search(r"^([\d\.]+)\s*(?:mah)?$", s, re.I)
        if m_mah:
            return int(round(float(m_mah.group(1))))

    # Voltage (V)
    if key == "voltage" or unit == "V":
        m_v = re.search(r"^([\d\.]+)\s*(?:v(?:dc)?)?$", s, re.I)
        if m_v:
            v_num = m_v.group(1)
            if v_num.endswith(".0"):
                v_num = v_num[:-2]
            return v_num

    # RPM
    if key == "rpm" or unit == "RPM":
        m_rpm = re.search(r"^(\d+)\s*(?:rpm)?$", s, re.I)
        if m_rpm:
            return int(m_rpm.group(1))

    # Current (A)
    if key == "current_a" or unit == "A":
        m_ma = re.search(r"^([\d\.]+)\s*ma$", s, re.I)
        if m_ma:
            return round(float(m_ma.group(1)) / 1000.0, 3)
        m_a = re.search(r"^([\d\.]+)\s*(?:a|amp|amps)?$", s, re.I)
        if m_a:
            return float(m_a.group(1))

    # Channels
    if key == "channels":
        m_ch = re.search(r"^(\d+)\s*(?:ch|channel|channels)?$", s, re.I)
        if m_ch:
            return int(m_ch.group(1))

    # NEMA
    if re.search(r"^nema\s*\d+$", s, re.I):
        return re.sub(r"\s+", "", s).lower()

    if v_type in ("integer", "float"):
        try:
            return int(s) if v_type == "integer" else float(s)
        except ValueError:
            pass

    return s.lower()

tests = [
    ("voltage", "24V", "enum", "V", "24"),
    ("voltage", "24 v", "enum", "V", "24"),
    ("voltage", "24VDC", "enum", "V", "24"),
    ("torque", "2.5kg-cm", "float", "kgcm", 2.5),
    ("torque", "2.5 kgcm", "float", "kgcm", 2.5),
    ("capacity_mah", "5200 mAh", "integer", "mAh", 5200),
    ("capacity_mah", "5.2Ah", "integer", "mAh", 5200),
    ("cells", "4S", "enum", None, "4s"),
    ("cells", "4 s", "enum", None, "4s"),
    ("cells", "4s lipo", "enum", None, "4s"),
    ("frame_size", "NEMA17", "string", None, "nema17"),
    ("frame_size", "nema 17", "string", None, "nema17"),
]

for k, val, vt, u, exp in tests:
    res = normalize_scalar(k, val, vt, u)
    passed = res == exp
    print(f"{k} -> '{val}': got {res} ({type(res).__name__}), expected {exp} -> {'PASS' if passed else 'FAIL'}")
    assert passed
print("\nALL NORMALIZATION TESTS PASSED!")
