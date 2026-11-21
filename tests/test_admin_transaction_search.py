"""Order searches use an isolated database, including public gateway references."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import patch

import mongomock
from bson import ObjectId
from flask import Flask


class AdminTransactionSearchTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test
        with patch.dict(sys.modules, {'db': types.SimpleNamespace(db=self.db)}):
            spec = importlib.util.spec_from_file_location('admin_transaction_search_test',
                Path(__file__).resolve().parents[1] / 'admin_transactions.py')
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.app.register_blueprint(self.module.admin_transactions_bp)
        self.app.add_url_rule('/login', 'login.login', lambda: 'login')
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
        self.render = patch.object(self.module, 'render_template', return_value='rendered').start()
        self.addCleanup(patch.stopall)

    def search(self, **args):
        self.assertEqual(self.client.get('/admin/transactions', query_string=args).status_code, 200)
        return self.render.call_args.kwargs

    def test_direct_order_reference_is_case_insensitive_and_trimmed(self):
        self.db.transactions.insert_many([{'reference': 'HAN123'}, {'reference': 'HAN1234'}])
        result = self.search(order_id=' han123 ')
        self.assertEqual([t['reference'] for t in result['transactions']], ['HAN123'])
        self.assertEqual(result['order_id'], 'han123')

    def test_public_and_split_orders_resolve_gateway_and_primary_references(self):
        self.db.orders.insert_one({'order_id': 'HAN123-2', 'payment_reference': 'PAYSTACK123',
            'debug': {'bulk_group_order_id': 'HAN123'}})
        self.db.transactions.insert_many([{'reference': 'PAYSTACK123'}, {'reference': 'HAN123'},
                                         {'reference': 'unrelated'}])
        result = self.search(order_id='HAN123-2')
        self.assertEqual({t['reference'] for t in result['transactions']}, {'PAYSTACK123', 'HAN123'})

    def test_search_combines_customer_dates_and_pagination(self):
        user_id = ObjectId()
        self.db.transactions.insert_many([{'reference': 'HAN123', 'user_id': user_id,
            'verified_at': datetime(2026, 11, 20)} for _ in range(11)] + [
            {'reference': 'HAN123', 'user_id': ObjectId(), 'verified_at': datetime(2026, 11, 20)},
            {'reference': 'HAN123', 'user_id': user_id, 'verified_at': datetime(2026, 11, 19)}])
        result = self.search(order_id='HAN123', customer=str(user_id), start_date='2026-11-20',
                             end_date='2026-11-20', page=2)
        self.assertEqual(result['total_pages'], 2)
        self.assertEqual(len(result['transactions']), 1)

    def test_unknown_or_regex_input_does_not_match_every_transaction(self):
        self.db.transactions.insert_one({'reference': 'HAN123'})
        self.assertEqual(self.search(order_id='.*')['transactions'], [])
        self.assertEqual(self.search(order_id='missing')['transactions'], [])
        self.assertEqual(len(self.search()['transactions']), 1)

    def test_requires_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/admin/transactions?order_id=HAN123').status_code, 302)
        self.render.assert_not_called()


if __name__ == '__main__':
    unittest.main()
