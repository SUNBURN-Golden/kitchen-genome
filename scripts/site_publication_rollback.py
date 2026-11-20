"""Roll back one generated alumni entry. Ship the seed change through the daily build/PR flow."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'daemon'))
from kg_collect.site_publish import rollback  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument('--restaurant-id')
    select.add_argument('--bundle-id')
    select.add_argument('--review-id')
    parser.add_argument('--reason', required=True)
    parser.add_argument('--seed', type=Path, default=ROOT / 'data' / 'seed.json')
    parser.add_argument('--db', help='Collector DB (defaults to KG_SERVICE_DB when set)')
    args = parser.parse_args(argv)
    try:
        tomb = rollback(args.seed, args.restaurant_id or args.bundle_id or args.review_id, args.reason, db_path=args.db)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({'rolled_back': tomb['id']} if tomb else {'already_rolled_back': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
