"""Keep one Failed/No Receipt entry for 0599205294 / MTN NORMAL / 5GB.

Preview: python cleanup_duplicate_failed_receipts.py
Apply:   python cleanup_duplicate_failed_receipts.py --apply
The oldest matching entry is kept across index and store submissions.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re

from bson import json_util


def normalized_phone(value):
    value = re.sub(r'[\s+()-]', '', str(value or ''))
    return '0' + value[3:] if re.fullmatch(r'233[0-9]{9}', value) else value


def matches(entry):
    return (normalized_phone(entry.get('phone')) == '0599205294'
            and ' '.join(str(entry.get('service_name') or '').split()).casefold() == 'mtn normal'
            and re.sub(r'\s+', '', str(entry.get('offer') or '')).casefold() == '5gb')


def plan_cleanup(collection):
    keeper = None
    changes = []
    # Stable ordering makes repeated previews and runs select the same entry.
    for report in collection.find({}).sort([('submitted_at', 1), ('_id', 1)]):
        remaining = []
        removed = []
        for index, entry in enumerate(report.get('entries', [])):
            if matches(entry):
                if keeper is None:
                    keeper = (report['_id'], index)
                else:
                    removed.append(index)
                    continue
            remaining.append(entry)
        if removed:
            changes.append({'report': report, 'remaining': remaining, 'removed': removed})
    return keeper, changes


def apply_cleanup(database, changes, backup_path):
    # Receipt recovery links orders using report ID and array index. Refuse to
    # shift a surviving entry that already has recovery activity or linked orders.
    for change in changes:
        report, remaining = change['report'], change['remaining']
        if not remaining:
            continue
        for index, entry in enumerate(report['entries']):
            if index in change['removed'] or index < min(change['removed']):
                continue
            key = f'{report["_id"]}:{index}'
            if (any(field.startswith('placement_') or field in ('order_id', 'order_ids') for field in entry)
                    or database['orders'].find_one({'admin_payment_override.key': key}, {'_id': 1})):
                raise RuntimeError(f'Report {report["_id"]} has a linked recovery entry that would move. '
                                   'No cleanup was applied; review this mixed batch manually.')
    # Extended JSON preserves IDs and dates for recovery. Never overwrite a backup.
    with Path(backup_path).open('x', encoding='utf-8') as backup:
        backup.write(json_util.dumps([change['report'] for change in changes], indent=2))
    deleted = updated = 0
    for change in changes:
        report, remaining = change['report'], change['remaining']
        query = {'_id': report['_id'], 'entries': report['entries']}
        if remaining:
            fields = {'entries': remaining, 'entry_count': len(remaining)}
            if 'duplicate_keys' in report:
                fields['duplicate_keys'] = list(dict.fromkeys(
                    entry['phone'] + ':' + ' '.join(str(entry.get('service_name') or '').split()).casefold()
                    for entry in remaining))
            result = database['failed_receipts'].update_one(query, {'$set': fields})
            matched = result.matched_count
            updated += result.modified_count
        else:
            result = database['failed_receipts'].delete_one(query)
            matched = result.deleted_count
            deleted += result.deleted_count
        if not matched:
            raise RuntimeError(f'Report {report["_id"]} changed during cleanup. '
                               'Stopped to preserve the newer data. Rerun the preview.')
    return deleted, updated


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--apply', action='store_true', help='Back up the affected reports, then remove duplicates.')
    args = parser.parse_args()
    from db import db

    keeper, changes = plan_cleanup(db['failed_receipts'])
    print('Target: 0599205294 / MTN NORMAL / 5GB (all pages and statuses)')
    if keeper is None:
        print('No matching reports found. Nothing changed.')
        return
    print(f'Keeping report {keeper[0]}, entry {keeper[1] + 1}.')
    count = sum(len(change['removed']) for change in changes)
    print(f'Duplicate entries to remove: {count}; affected submissions: {len(changes)}.')
    if not changes:
        print('Only one matching report exists. Nothing changed.')
        return
    if not args.apply:
        print('Preview only. Run: python cleanup_duplicate_failed_receipts.py --apply')
        return
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup_path = Path(__file__).with_name(f'failed_receipts_backup_{stamp}.json')
    deleted, updated = apply_cleanup(db, changes, backup_path)
    print(f'Removed {count} duplicate entries. Deleted {deleted} empty submissions; updated {updated} mixed submissions.')
    print(f'Backup: {backup_path}')


if __name__ == '__main__':
    main()
