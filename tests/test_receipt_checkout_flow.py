"""Exercise real source checkout helpers against an isolated Mongo database."""
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import mongomock
from mongomock.gridfs import enable_gridfs_integration
from bson import ObjectId
from flask import Flask


class ReceiptCheckoutFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        enable_gridfs_integration()
        database = mongomock.MongoClient().checkout_tests
        with patch.dict(sys.modules, {'db': types.SimpleNamespace(db=database)}):
            cls.worker = importlib.import_module('checkout')
            cls.index = importlib.import_module('index')
            cls.store = importlib.import_module('routes.store_page')
            cls.recovery = importlib.import_module('receipt_recovery')
            cls.receipts = importlib.import_module('failed_receipts')
            cls.access = importlib.import_module('admin_access')

    def setUp(self):
        self.db = mongomock.MongoClient().test
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'templates'))
        self.app.secret_key = 'test'
        self.app.register_blueprint(self.index.index_bp)
        self.app.register_blueprint(self.store.stores_bp)
        self.app.register_blueprint(self.receipts.failed_receipts_bp)
        self.addCleanup(patch.stopall)
        patch.object(self.store, '_save_store_order_batch',
                     side_effect=lambda docs: self.db.orders.insert_many(docs)).start()
        for module in [self.worker, self.index, self.store]:
            for name in vars(module):
                if name.endswith('_col'):
                    old = getattr(module, name)
                    if isinstance(old, mongomock.collection.Collection):
                        patch.object(module, name, self.db[old.name]).start()
        for module in [self.index, self.store]:
            patch.object(module, '_known_number_validation_error', return_value=None).start()
            patch.object(module, '_verify_paystack', side_effect=AssertionError('Paystack must not run during an override')).start()
        self.service_id = ObjectId()
        self.owner_id = ObjectId()
        self.db.services.insert_one({'_id': self.service_id, 'name': 'MTN Normal', 'type': 'OFF',
            'network': 'MTN', 'service_network': 'MTN', 'unit': 'GB', 'status': 'OPEN',
            'availability': 'AVAILABLE', 'display': 'ON', 'public_display': 'ON', 'store_display': 'ON',
            'offers': [{'value': {'volume': 5000}, 'amount': 20}],
            'public_offers': [{'value': {'volume': 5000}, 'amount': 30}],
            'store_offers': [{'value': {'volume': 5000}, 'amount': 25}]})
        self.store_doc = {'slug': 'ama', 'name': 'Ama Store', 'owner_id': self.owner_id,
                          'status': 'published', 'pricing': {'per_service': [
                              {'service_id': str(self.service_id), 'offers': [{'index': 0, 'total': 30}]}]}}
        self.db.stores.insert_one(self.store_doc.copy())
        self.cart = [{'serviceId': str(self.service_id), 'serviceName': 'MTN Normal', 'phone': '0240000000',
                      'value': '5GB', 'value_obj': {'volume': 5000}, 'amount': 30, 'base_amount': 25}]
        self.override = {'key': 'test-report:0', 'report_id': 'test-report', 'entry_index': 0,
                         'admin_id': 'admin', 'customer_id': None, 'reason': 'Failed Receipt / No Receipt'}

    def test_index_creates_normal_order_without_fake_payment_or_wallet_debit(self):
        with self.app.test_request_context('/'):
            response, code = self.recovery._checkout(self.index, None, self.cart, self.override)
        self.assertEqual(code, 200)
        self.assertTrue(response['success'])
        order = self.db.orders.find_one()
        self.assertEqual(order['items'][0]['phone'], '0240000000')
        self.assertEqual(order['total_amount'], 30)
        self.assertEqual(order['channel'], 'public_web')
        self.assertEqual(order['payment_status'], 'admin_override')
        self.assertEqual(order['amount_collected'], 0)
        self.assertEqual(order['admin_payment_override']['key'], 'test-report:0')
        self.assertNotIn('paystack_reference', order)
        transaction = self.db.transactions.find_one()
        self.assertEqual(transaction['amount'], 0)
        self.assertEqual(transaction['order_value'], 30)
        self.assertEqual(transaction['type'], 'admin_override')
        self.assertEqual(self.db.balances.count_documents({}), 0)

    def test_store_preserves_pricing_profit_credit_and_override_audit(self):
        with self.app.test_request_context('/'):
            response, code = self.recovery._checkout(self.store, self.store_doc, self.cart, self.override)
        self.assertEqual(code, 200)
        self.assertTrue(response['success'])
        order = self.db.orders.find_one()
        expected_profit = order['items'][0]['store_profit_amount']
        self.assertGreater(expected_profit, 0)
        self.assertEqual(order['store_slug'], 'ama')
        self.assertEqual(order['store_owner_id'], self.owner_id)
        self.assertEqual(order['channel'], 'store_checkout')
        self.assertEqual(order['payment_status'], 'admin_override')
        self.assertEqual(order['amount_collected'], 0)
        self.assertNotIn('paystack_reference', order)
        account = self.db.store_accounts.find_one({'store_slug': 'ama'})
        self.assertEqual(account['total_profit_balance'], expected_profit)
        self.assertEqual(self.store._credit_paid_store_order_profits('ama', [order], 'admin_override'), 0)
        self.assertEqual(self.store._credit_paid_store_order_profits('ama', [order], 'ADMIN_OVERRIDE'), 0)
        self.assertEqual(self.db.store_accounts.find_one()['total_profit_balance'], expected_profit)
        self.assertEqual(self.db.transactions.find_one()['amount'], 0)
        self.assertEqual(self.db.transactions.find_one()['type'], 'admin_override')
        self.assertEqual(self.db.balances.count_documents({}), 0)

    def test_override_profit_requires_audited_order_and_unpaid_still_cannot_credit(self):
        order = {'order_id': 'unaudited', 'items': [{'store_profit_amount': 5}]}
        self.assertEqual(self.store._credit_paid_store_order_profits('ama', [order], 'admin_override'), 0)
        self.assertEqual(self.store._credit_paid_store_order_profits('ama', [order], 'unpaid'), 0)
        self.assertEqual(self.db.store_accounts.count_documents({}), 0)

    def test_normal_store_checkout_keeps_paid_order_and_debit_transaction(self):
        with self.app.test_request_context('/'):
            response, code = self.store.place_store_order({'store_doc': self.store_doc,
                'cart': self.cart, 'payment_status': 'paid', 'paid_from': 'paystack_inline',
                'paystack_reference': 'NORMAL-REF'}, channel='store_checkout')
        self.assertTrue(response['success'])
        order = self.db.orders.find_one()
        transaction = self.db.transactions.find_one()
        self.assertEqual(order['payment_status'], 'paid')
        self.assertEqual(order['paystack_reference'], 'NORMAL-REF')
        self.assertNotIn('admin_payment_override', order)
        self.assertEqual(transaction['type'], 'debit')
        self.assertEqual(transaction['amount'], 30)
        self.assertEqual(self.db.store_accounts.find_one()['total_profit_balance'], 5)

    def test_normal_index_checkout_still_verifies_payment(self):
        with self.app.test_request_context('/'), patch.object(self.index, '_verify_paystack',
                return_value=(True, {'amount': 3060, 'currency': 'GHS', 'status': 'success'}, '')) as verify:
            result = self.index.finalize_paid_order('NORMAL-REF', {'cart': self.cart})
        self.assertTrue(result['success'])
        verify.assert_called_once_with('NORMAL-REF')
        order = self.db.orders.find_one()
        self.assertEqual(order['payment_status'], 'paid')
        self.assertEqual(order['paid_from'], 'public_paystack')
        self.assertEqual(order['paystack_reference'], 'NORMAL-REF')
        self.assertNotIn('admin_payment_override', order)
        self.assertGreater(self.db.transactions.find_one()['amount'], 0)

    def test_both_source_helpers_still_submit_the_same_provider_worker(self):
        self.db.services.update_one({'_id': self.service_id}, {'$set': {'type': 'API', 'provider': 'bundleportal'}})
        # Only the provider network request is replaced; checkout.py's worker
        # persists its normal API statuses and references.
        with patch.object(self.worker, '_send_bundleportal_order', return_value=(True, {'success': True, 'reference': 'provider-test'})) as provider:
            with self.app.test_request_context('/'):
                index_response, _ = self.recovery._checkout(self.index, None, self.cart, self.override)
                store_override = dict(self.override, key='test-report:1', entry_index=1)
                store_response, _ = self.recovery._checkout(self.store, self.store_doc, self.cart, store_override)
            self.assertTrue(index_response['success'])
            self.assertTrue(store_response['success'])
            self.assertEqual(provider.call_count, 2)
            orders = list(self.db.orders.find({}))
            self.assertEqual(len(orders), 2)
            for order in orders:
                self.assertEqual(order['items'][0]['provider'], 'bundleportal')
                self.assertNotEqual(order['items'][0]['api_status'], 'submitting')

    def test_public_endpoints_do_not_accept_override_flags(self):
        client = self.app.test_client()
        public = client.post('/public-checkout', json={'cart': self.cart, 'admin_override': True})
        self.assertFalse(public.get_json()['success'])
        store = client.post('/store-checkout/ama', json={'cart': self.cart, 'method': 'admin_override', 'admin_payment_override': self.override})
        self.assertEqual(store.status_code, 400)
        self.assertFalse(store.get_json()['success'])
        self.assertEqual(self.db.orders.count_documents({}), 0)

    def test_telecel_admin_override_posts_to_v2_on_both_source_pages(self):
        from unittest.mock import Mock
        self.cart = [dict(item, serviceName='TELECEL', network='telecel') for item in self.cart]
        self.db.services.update_one({'_id': self.service_id}, {'$set': {
            'name': 'TELECEL', 'network': 'telecel', 'service_network': 'telecel',
            'type': 'API', 'provider': 'bundleportal'}})
        response = Mock(status_code=200, text='')
        response.json.return_value = {'success': True, 'data': {
            'reference': 'TEST-TELECEL', 'status': 'processing'}}
        with patch.object(self.worker, 'BUNDLEPORTAL_API_KEY', 'test-key'), patch.object(
            self.worker.requests, 'post', return_value=response
        ) as post:
            with self.app.test_request_context('/'):
                index_result, index_code = self.recovery._checkout(self.index, None, self.cart, self.override)
                store_result, store_code = self.recovery._checkout(self.store, self.store_doc,
                    self.cart, dict(self.override, key='test-report:telecel', entry_index=1))
        self.assertEqual((200, 200), (index_code, store_code))
        self.assertTrue(index_result['success'] and store_result['success'])
        self.assertEqual(2, post.call_count)
        for call in post.call_args_list:
            self.assertEqual('https://api.bundleportal.com/v2', call.args[0])
            body = call.kwargs['json']
            self.assertEqual('place_order', body['action'])
            self.assertEqual('telecel', body['network'])
            self.assertTrue(body['order_id'])
        for order in self.db.orders.find({}):
            self.assertEqual('admin_override', order['payment_status'])
            self.assertEqual(0, order['amount_collected'])
            self.assertEqual('TEST-TELECEL', order['items'][0]['provider_reference'])

    def test_original_source_templates_render_automatic_cart_bridge(self):
        config = {'cart': [dict(self.cart[0], value_text='5GB')], 'place_url': '/admin/place',
                  'token': 'test', 'csrf_token': 'test', 'return_url': '/admin/failed-receipts'}
        with self.app.test_request_context('/'):
            index_html = self.index.render_index_page(receipt_recovery=config)
            store_html = self.store.render_store_page(self.store_doc, receipt_recovery=config)
        for html in [index_html, store_html]:
            self.assertIn('Placing reported order', html)
            self.assertIn('window.runReceiptRecovery', html)
            self.assertIn('receipt_recovery_', html)
            self.assertIn('No new payment will be collected.', html)
        with self.app.test_request_context('/'):
            normal_html = self.index.render_index_page()
        self.assertNotIn('window.runReceiptRecovery =', normal_html)

    def test_managed_admin_needs_receipt_page_permission(self):
        app = Flask(__name__, template_folder=self.app.template_folder)
        app.secret_key = 'test'
        patch.object(self.access, 'db', self.db).start()
        self.access.init_admin_access(app)
        app.register_blueprint(self.receipts.failed_receipts_bp)
        app.add_url_rule('/login', 'login.login', lambda: 'login')
        app.add_url_rule('/no-access', 'admin_users.no_access', lambda: 'denied')
        for _, _, endpoint, _ in self.access.ADMIN_PAGES:
            if not endpoint.startswith('failed_receipts.'):
                app.add_url_rule('/test/' + endpoint, endpoint, lambda: 'test')
        admin_id = self.db.users.insert_one({'role': 'admin', 'managed_admin': True, 'allowed_pages': ['orders'], 'status': 'active'}).inserted_id
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(role='admin', user_id=str(admin_id), receipt_csrf='test')
        self.assertEqual(client.post('/admin/failed-receipts/report/0/start', json={'csrf_token': 'test'}).status_code, 403)
        self.assertEqual(client.get('/admin/failed-receipts/report/0/source', headers={'Accept': 'application/json'}).status_code, 403)
        self.db.users.update_one({'_id': admin_id}, {'$set': {'allowed_pages': ['failed_receipts']}})
        # Permission succeeds; the nonexistent report correctly returns 404.
        self.assertEqual(client.post('/admin/failed-receipts/report/0/start', json={'csrf_token': 'test'}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
