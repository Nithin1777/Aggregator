"""
Convert a vendor's historically stored ex-GST prices to tax-inclusive prices.

Usage:
    python scripts/apply_vendor_gst.py --vendor quartzcomponents --rate 0.18
    python scripts/apply_vendor_gst.py --vendor quartzcomponents --rate 0.18 --dry-run

Idempotent: rows already carrying `extra.gst_rate` are skipped. The original
amount is preserved in `extra.price_ex_gst`.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", required=True, help="Vendor slug, e.g. quartzcomponents")
    parser.add_argument("--rate", type=float, default=0.18, help="GST rate, default 0.18")
    parser.add_argument("--dry-run", action="store_true", help="Report rows without writing")
    args = parser.parse_args()

    db.init_db()
    result = db.apply_vendor_gst(args.vendor, args.rate, dry_run=args.dry_run)
    print(
        f"{result['vendor']}: updated={result['updated']} skipped={result['skipped']} "
        f"rate={result['rate']} dry_run={result['dry_run']}"
    )


if __name__ == "__main__":
    main()
