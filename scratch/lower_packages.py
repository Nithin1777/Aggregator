import yaml
from pathlib import Path

path = Path("config/filter_extension.yaml")
data = yaml.safe_load(path.read_text(encoding="utf-8"))
for cat in data.get("categories", []):
    for attr in cat.get("attributes", []):
        if attr.get("key") == "package" and attr.get("values"):
            attr["values"] = [str(v).lower() for v in attr["values"]]
header = """# Filter extension: vendor-backed, production-facing spec filters.
#
# These attributes are merged into config/taxonomy.yaml at load time and are
# populated by scripts/promote_vendor_specs.py.
"""
path.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
print("updated", path)
