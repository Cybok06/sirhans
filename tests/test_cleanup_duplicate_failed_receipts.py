from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

import mongomock
from bson import json_util

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleanup_duplicate_failed_receipts import apply_cleanup, plan_cleanup


class CleanupFailedReceiptsTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test
        self.entry = {'phone': '0599205294', 'service_name': 'MTN NORMAL', 'offer': '5GB', 'status': 'unresolved'}
        self.other = dict(self.entry, service_name='MTN EXPRESS')
        self.db.failed_receipts.insert_one({'_id': 'oldest', 'submitted_at': datetime(2026, 1, 1, tzinfo=timezone.utc),
                                            'entries': [self.entry], 'entry_count': 1})

    def test_cleanup_preserves_oldest_other_services_and_backup(self):
        self.db.failed_receipts.insert_many([
            {'_id': 'duplicate', 'submitted_at': datetime(2026, 2, 1), 'entries': [self.entry]},
            {'_id': 'mixed', 'submitted_at': datetime(2026, 3, 1),
             'entries': [dict(self.entry, phone='+233599205294'), self.other, dict(self.entry, offer='10GB')],
             'entry_count': 3, 'duplicate_keys': ['0599205294:mtn normal', '0599205294:mtn express']}])
        keeper, changes = plan_cleanup(self.db.failed_receipts)
        self.assertEqual(keeper, ('oldest', 0))
        self.assertEqual(len(changes), 2)
        self.assertEqual(self.db.failed_receipts.count_documents({}), 3)  # Preview is read-only.
        with TemporaryDirectory() as folder:
            backup = Path(folder) / 'backup.json'
            self.assertEqual(apply_cleanup(self.db, changes, backup), (1, 1))
            saved = json_util.loads(backup.read_text(encoding='utf-8'))
            self.assertEqual({doc['_id'] for doc in saved}, {'duplicate', 'mixed'})
        mixed = self.db.failed_receipts.find_one({'_id': 'mixed'})
        self.assertEqual(mixed['entries'], [self.other, dict(self.entry, offer='10GB')])
        self.assertEqual(mixed['entry_count'], 2)
        self.assertEqual(plan_cleanup(self.db.failed_receipts)[1], [])

    def test_repeated_entries_inside_one_submission(self):
        self.db.failed_receipts.update_one({}, {'$set': {'entries': [self.entry, self.entry]}})
        _, changes = plan_cleanup(self.db.failed_receipts)
        with TemporaryDirectory() as folder:
            apply_cleanup(self.db, changes, Path(folder) / 'backup.json')
        self.assertEqual(self.db.failed_receipts.find_one()['entries'], [self.entry])

    def test_concurrent_admin_update_is_preserved(self):
        self.db.failed_receipts.insert_one({'_id': 'new', 'submitted_at': datetime(2026, 2, 1), 'entries': [self.entry]})
        _, changes = plan_cleanup(self.db.failed_receipts)
        self.db.failed_receipts.update_one({'_id': 'new'}, {'$set': {'entries.0.status': 'resolved'}})
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'changed during cleanup'):
                apply_cleanup(self.db, changes, Path(folder) / 'backup.json')
        self.assertEqual(self.db.failed_receipts.count_documents({}), 2)

    def test_linked_recovery_entry_cannot_be_shifted(self):
        self.db.failed_receipts.insert_one({'_id': 'mixed', 'submitted_at': datetime(2026, 2, 1),
                                            'entries': [self.entry, self.other]})
        self.db.orders.insert_one({'admin_payment_override': {'key': 'mixed:1'}})
        _, changes = plan_cleanup(self.db.failed_receipts)
        with TemporaryDirectory() as folder:
            backup = Path(folder) / 'backup.json'
            with self.assertRaisesRegex(RuntimeError, 'linked recovery entry'):
                apply_cleanup(self.db, changes, backup)
            self.assertFalse(backup.exists())
        self.assertEqual(len(self.db.failed_receipts.find_one({'_id': 'mixed'})['entries']), 2)


if __name__ == '__main__':
    unittest.main()
