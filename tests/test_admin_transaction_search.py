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

    def test_customer_and_date_filters_still_work_without_order_id(self):
        user_id = ObjectId()
        self.db.transactions.insert_many([{'reference': 'HAN123', 'user_id': user_id,
            'verified_at': datetime(2026, 11, 20)} for _ in range(11)] + [
            {'reference': 'HAN123', 'user_id': ObjectId(), 'verified_at': datetime(2026, 11, 20)},
            {'reference': 'HAN123', 'user_id': user_id, 'verified_at': datetime(2026, 11, 19)}])
        result = self.search(customer=str(user_id), start_date='2026-11-20',
                             end_date='2026-11-20', page=2)
        self.assertEqual(result['total_pages'], 2)
        self.assertEqual(len(result['transactions']), 1)

    def test_guest_order_lookup_ignores_stale_customer_and_date_filters(self):
        reference = 'SIRHANS_1794985473959_XY47LI9M'
        self.db.orders.insert_one({'order_id': 'HAN7783132', 'paystack_reference': reference,
                                   'debug': {'bulk_group_order_id': 'HAN1258202'}})
        self.db.transactions.insert_one({'reference': reference, 'user_id': None,
            'source': 'paystack_inline', 'status': 'success', 'amount': 4.46,
            'verified_at': datetime(2026, 11, 18, 7, 5, 2)})
        result = self.search(order_id='HAN7783132', customer=str(ObjectId()),
                             start_date='2026-11-21', end_date='2026-11-21', page=9)
        self.assertEqual([t['reference'] for t in result['transactions']], [reference])
        self.assertEqual(result['selected_customer'], '')
        self.assertEqual(result['start_date'], '')
        self.assertEqual(result['end_date'], '')
        self.assertEqual(result['page'], 1)

    def test_unknown_or_regex_input_does_not_match_every_transaction(self):
        self.db.transactions.insert_one({'reference': 'HAN123'})
        self.assertEqual(self.search(order_id='.*')['transactions'], [])
        self.assertEqual(self.search(order_id='missing')['transactions'], [])
        self.assertEqual(len(self.search()['transactions']), 1)

    def test_order_ids_and_sources_resolve_public_store_and_dashboard_orders(self):
        self.db.orders.insert_many([
            {'order_id': 'HAN7783132', 'paystack_reference': 'PAYSTACK123',
             'paid_from': 'public_paystack', 'channel': 'public_web'},
            {'order_id': 'HANSTORE', 'store_slug': 'brains-data-hub', 'channel': 'store_checkout'},
            {'order_id': 'HANWALLET', 'paid_from': 'wallet'},
        ])
        self.db.transactions.insert_many([
            {'reference': 'PAYSTACK123', 'user_id': None, 'type': 'debit', 'source': 'paystack_inline'},
            {'reference': 'HANSTORE', 'type': 'debit', 'meta': {'store_checkout': True}},
            {'reference': 'HANWALLET', 'type': 'purchase'},
            {'reference': 'TOPUP123', 'type': 'deposit'},
        ])
        rows = {t['reference']: t for t in self.search()['transactions']}
        for reference, order_id, source in (
                ('PAYSTACK123', 'HAN7783132', 'Index Page'),
                ('HANSTORE', 'HANSTORE', 'Store'),
                ('HANWALLET', 'HANWALLET', 'Customer Dashboard')):
            self.assertEqual(rows[reference]['order_ids'], [order_id])
            self.assertEqual(rows[reference]['source_label'], source)
        self.assertEqual(rows['TOPUP123']['order_ids'], [])
        self.assertEqual(rows['TOPUP123']['source_label'], 'Account Deposit')

    def test_split_order_ids_are_all_shown_without_unrelated_orders(self):
        self.db.orders.insert_many([
            {'order_id': 'HAN123', 'paystack_reference': 'PAY123',
             'debug': {'bulk_group_order_id': 'HAN123'}, 'channel': 'public_web'},
            {'order_id': 'HAN456', 'payment_reference': 'PAY123',
             'debug': {'bulk_group_order_id': 'HAN123'}, 'channel': 'public_web'},
            {'order_id': 'HAN999', 'payment_reference': 'OTHER'},
        ])
        self.db.transactions.insert_one({'reference': 'PAY123', 'type': 'debit'})
        row = self.search()['transactions'][0]
        self.assertEqual(row['order_ids'], ['HAN123', 'HAN456'])
        self.assertEqual(row['source_label'], 'Index Page')

    def test_source_uses_origin_instead_of_logged_in_customer_or_payment_channel(self):
        rows = [
            {'user_id': ObjectId(), 'type': 'debit', 'source': 'paystack_inline'},
            {'type': 'debit', 'channel': 'arkesel_ussd'},
            {'type': 'debit', 'channel': 'arkesel_ussd', 'meta': {'store_slug': 'ama'}},
            {'type': 'debit', 'channel': 'mobile_money'},
        ]
        self.module._attach_order_details(rows)
        self.assertEqual([r['source_label'] for r in rows], ['Index Page', 'USSD', 'Store', 'Other'])

    def test_table_renders_order_id_separately_from_payment_reference(self):
        from flask import render_template
        from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'admin_sidebar.html': ''}),
            FileSystemLoader(str(Path(__file__).resolve().parents[1] / 'templates'))])
        self.render.side_effect = render_template
        self.db.orders.insert_one({'order_id': 'HAN7783132', 'paystack_reference': 'PAYSTACK123',
                                   'channel': 'public_web'})
        self.db.transactions.insert_one({'reference': 'PAYSTACK123', 'amount': 4.46, 'type': 'debit'})
        html = self.client.get('/admin/transactions').get_data(as_text=True)
        self.assertIn('>Order ID</th>', html)
        self.assertIn('HAN7783132</div>', html)
        self.assertIn('PAYSTACK123</span>', html)
        self.assertIn('Index Page', html)

    def test_requires_admin(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/admin/transactions?order_id=HAN123').status_code, 302)
        self.render.assert_not_called()


if __name__ == '__main__':
    unittest.main()
