"""
Backfills the canonical category column for every indexed product.

Usage:
    python scripts/backfill_categories.py            # only rows without a category
    python scripts/backfill_categories.py --force    # re-classify everything
    python scripts/backfill_categories.py --report   # print the distribution

The taxonomy lives in `config/taxonomy.yaml`; this script applies it to
`products.category_canonical` and records the raw -> canonical mapping in
`category_map` so it stays auditable.
"""
import argparse
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

import db  # noqa: E402
from src.spec.taxonomy_loader import get_taxonomy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-classify every product, not only the ones without a category.",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print the resulting category distribution and exit.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Rows per transaction (default: 5000).",
    )
    args = parser.parse_args()

    db.init_db()

    if not args.report:
        if args.force:
            print("Re-classifying the full catalog...")
            with db.get_db_connection() as conn:
                conn.execute("UPDATE products SET category_canonical = NULL;")
                conn.commit()
        else:
            print("Classifying products that have no canonical category yet...")

        stats = db.backfill_categories(batch_size=args.batch_size)
        print(
            f"Scanned {stats['scanned']:,} products, updated {stats['updated']:,}, "
            f"unmatched {stats['unmatched']:,}."
        )

    taxonomy = get_taxonomy()
    counts = db.get_category_counts()
    total = sum(counts.values())
    print(f"\nCanonical categories ({total:,} products):\n")
    for cat in taxonomy.categories.values():
        n = counts.get(cat.id, 0)
        share = (n * 100.0 / total) if total else 0.0
        bar = "#" * int(share / 2)
        print(f"  {cat.id:<22} {n:>7,}  {share:5.1f}%  {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
