"""Reindex stored descriptions, titles and attributes into searchable evidence.

No vendor requests. Dry-run is the default. --apply uses a SQLite backup before
the first write, commits complete batches, and can be resumed after interruption.
All source listings and descriptions are retained.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.spec.enrichment import ensure_enrichment_schema, enrich_listing, refresh_statistics


def backfill_catalog(conn, *, apply=False, after_id=0, limit=None, batch_size=200):
    if batch_size < 1 or after_id < 0 or (limit is not None and limit < 1):
        raise ValueError('Invalid batch size, limit, or starting ID')
    stats = {'scanned':0, 'changed':0, 'last_id':after_id, 'mode':'apply' if apply else 'dry_run'}
    if not apply:
        total = conn.execute('SELECT COUNT(*) FROM products WHERE id>?', (after_id,)).fetchone()[0]
        stats['eligible_products'] = min(total,limit) if limit else total
        stats['descriptions_available'] = conn.execute('SELECT COUNT(*) FROM product_descriptions WHERE product_id>?', (after_id,)).fetchone()[0]
        stats['note'] = 'No writes. --apply regenerates derived rows and usable search metadata from existing evidence.'
        return stats
    ensure_enrichment_schema(conn)
    all_keys = set()
    while limit is None or stats['scanned'] < limit:
        size = min(batch_size, limit-stats['scanned']) if limit else batch_size
        ids = [r[0] for r in conn.execute('SELECT id FROM products WHERE id>? ORDER BY id LIMIT ?', (stats['last_id'],size))]
        if not ids:
            break
        keys, vendor_keys = set(), set()
        with conn:
            for product_id in ids:
                result = enrich_listing(conn, product_id)
                stats['changed'] += int(result['changed'])
                keys.update(result['keys']); vendor_keys.update(result['vendor_keys'])
            all_keys.update(keys)
        stats['scanned'] += len(ids)
        stats['last_id'] = ids[-1]
        print(json.dumps(stats), flush=True)
    # A bulk repair updates the normalized rows in resumable batches, then
    # publishes statistics once. Repeatedly rescanning every popular key after
    # each batch makes real catalogs orders of magnitude slower than fixtures.
    from src.spec.vendor_index import recompute_vendor_stats
    all_keys.update(row[0] for row in conn.execute('SELECT DISTINCT key FROM product_specs'))
    with conn:
        refresh_statistics(conn, all_keys, set())
        recompute_vendor_stats(conn, commit=False)
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT/'data/components.db')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--after-id', type=int, default=0)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--batch-size', type=int, default=200)
    args = parser.parse_args(argv)
    path = args.db.resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA temp_store=MEMORY')
    try:
        if args.apply:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = path.parent/'backups'/f'{path.stem}-before-enrichment-{stamp}.sqlite'
            backup.parent.mkdir(parents=True,exist_ok=True)
            with sqlite3.connect(backup) as destination:
                conn.backup(destination)
            print(json.dumps({'backup':str(backup)}), flush=True)
        print(json.dumps(backfill_catalog(conn,apply=args.apply,after_id=args.after_id,
                                          limit=args.limit,batch_size=args.batch_size)), flush=True)
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
