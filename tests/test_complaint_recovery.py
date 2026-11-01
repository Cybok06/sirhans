import importlib
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import patch

import mongomock
from bson import ObjectId
from flask import Blueprint
from jinja2 import ChoiceLoader, DictLoader

import test_receipt_checkout_flow as checkout_tests


class ComplaintRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        checkout_tests.ReceiptCheckoutFlowTests.setUpClass.__func__(cls)
        with patch.dict(sys.modules, {'db': types.SimpleNamespace(db=mongomock.MongoClient().test),
                'checkout': cls.worker, 'receipt_recovery': cls.recovery}):
            cls.admin = importlib.import_module('admin_complaints')
            cls.complaint_helpers = cls.admin.complaint_recovery
            cls.dashboard = importlib.import_module('customer_dashboard')

    def setUp(self):
        checkout_tests.ReceiptCheckoutFlowTests.setUp(self)
        patch.dict(sys.modules, {'checkout': self.worker, 'index': self.index,
            'customer_dashboard': self.dashboard, 'routes.store_page': self.store,
            'db': types.SimpleNamespace(db=self.db)}).start()
        for module in [self.admin, self.dashboard, self.recovery, self.complaint_helpers]:
            if hasattr(module, 'db'):
                patch.object(module, 'db', self.db).start()
            for name in vars(module):
                old = getattr(module, name)
                if name.endswith('_col') and isinstance(old, mongomock.collection.Collection):
                    patch.object(module, name, self.db[old.name]).start()
        patch.object(self.worker, '_known_number_validation_error', return_value=None).start()
        patch.object(self.admin, '_send_sms', return_value='sent').start()
        patch.object(self.worker, 'jlog').start()
        patch.object(self.index, 'jlog').start()
        patch.object(self.store, 'jlog').start()
        self.app.register_blueprint(self.worker.checkout_bp)
        self.app.register_blueprint(self.dashboard.customer_dashboard_bp)
        self.app.add_url_rule('/test/afa', 'afa.afa_list_page', lambda: 'afa')
        bp = Blueprint('admin_complaints', __name__)
        bp.add_url_rule('/admin/complaints', 'admin_view_complaints', self.admin.admin_view_complaints)
        bp.add_url_rule('/admin/complaints/<complaint_id>/update', 'update_complaint_status', self.admin.update_complaint_status, methods=['POST'])
        self.complaint_helpers.register_complaint_recovery(bp, self.db.complaints)
        self.app.register_blueprint(bp)
        # Source layouts are real; sidebar navigation is unrelated to checkout.
        self.app.jinja_loader = ChoiceLoader([DictLoader({'admin_sidebar.html': '', 'customer_sidebar.html': ''}), self.app.jinja_loader])
        self.app.context_processor(lambda: {'can_access_page': lambda key: True})
        self.client = self.app.test_client()
        self.customer_id = self.db.users.insert_one({'role': 'customer', 'username': 'Buyer', 'agent_level': 'normal'}).inserted_id
        self.db.balances.insert_one({'user_id': self.customer_id, 'amount': 0})
        self.db.users.insert_one({'_id': self.owner_id, 'role': 'admin', 'status': 'active'})
        with self.client.session_transaction() as session:
            session.update(role='admin', user_id=str(self.owner_id), receipt_csrf='csrf')

    def complaint(self, origin='customer'):
        original = {'user_id': self.customer_id, 'order_id': 'ORIGINAL', 'items': self.cart,
                    'status': 'failed', 'created_at': datetime.utcnow(),
                    'paid_from': 'wallet' if origin == 'customer' else 'public_paystack',
                    'channel': 'customer_dashboard' if origin == 'customer' else 'public_web'}
        if origin == 'store':
            original.update(store_slug='ama', channel='store_checkout', paid_from='paystack_inline')
        oid = self.db.orders.insert_one(original).inserted_id
        complaint_id = self.db.complaints.insert_one({'user_id': self.customer_id,
            'order_ref': {'_id': oid, 'order_id': 'ORIGINAL'}, 'service_name': 'MTN Normal',
            'offer': '5GB', 'status': 'pending', 'submitted_at': datetime.utcnow()}).inserted_id
        return str(complaint_id)

    def start(self, complaint_id):
        return self.client.post(f'/admin/complaints/order-recovery/{complaint_id}/0/start', data={'csrf_token': 'csrf'})

    def place(self, complaint_id):
        with self.client.session_transaction() as session:
            token = session['receipt_recovery_tickets'][f'complaint:{complaint_id}:0']['token']
        return self.client.post(f'/admin/complaints/order-recovery/{complaint_id}/0/place',
            json={'csrf_token': 'csrf', 'token': token, 'cart': [{'phone': 'forged'}]})

    def test_customer_origin_uses_real_wallet_checkout_without_debit_or_cart_clear(self):
        complaint_id = self.complaint()
        self.db.carts.insert_one({'user_id': self.customer_id, 'items': [{'phone': 'unrelated'}]})
        self.db.services.update_one({'_id': self.service_id}, {'$set': {'elite_offers': [{'amount': 18}]}})
        self.db.users.update_one({'_id': self.customer_id}, {'$set': {'agent_level': 'elite'}})
        response = self.start(complaint_id)
        self.assertTrue(response.location.endswith('/source'))
        page = self.client.get(response.location)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Return to Customer Complaints', page.get_data(as_text=True))
        result = self.place(complaint_id)
        self.assertTrue(result.get_json()['success'])
        self.assertEqual(result.get_json()['return_url'], '/admin/complaints')
        complaint = self.db.complaints.find_one()
        self.assertEqual(complaint['status'], 'resolved')
        order = self.db.orders.find_one({'admin_payment_override.key': f'complaint:{complaint_id}:0'})
        self.assertEqual(order['user_id'], self.customer_id)
        self.assertEqual(order['items'][0]['phone'], '0240000000')
        self.assertEqual(order['total_amount'], 18)
        self.assertEqual(order['payment_status'], 'admin_override')
        self.assertEqual(order['amount_collected'], 0)
        self.assertEqual(self.db.balances.find_one()['amount'], 0)
        self.assertEqual(self.db.carts.find_one()['items'], [{'phone': 'unrelated'}])
        self.assertEqual(self.db.transactions.find_one()['amount'], 0)
        self.assertEqual(self.db.failed_receipts.count_documents({}), 0)
        self.assertTrue(self.place(complaint_id).get_json()['success'])
        self.assertEqual(self.db.orders.count_documents({'admin_payment_override.key': f'complaint:{complaint_id}:0'}), 1)

    def test_index_origin_uses_index_flow_and_returns_to_complaints(self):
        complaint_id = self.complaint('index')
        response = self.start(complaint_id)
        self.assertEqual(self.client.get(response.location).status_code, 200)
        self.assertTrue(self.place(complaint_id).get_json()['success'])
        order = self.db.orders.find_one({'admin_payment_override.key': f'complaint:{complaint_id}:0'})
        self.assertEqual(order['channel'], 'public_web')
        self.assertEqual(order['total_amount'], 30)
        self.assertEqual(order['admin_payment_override']['reason'], 'Customer complaint')

    def test_store_origin_keeps_store_profit_credit_once(self):
        complaint_id = self.complaint('store')
        response = self.start(complaint_id)
        self.assertEqual(self.client.get(response.location).status_code, 200)
        self.assertTrue(self.place(complaint_id).get_json()['success'])
        order = self.db.orders.find_one({'admin_payment_override.key': f'complaint:{complaint_id}:0'})
        self.assertEqual(order['store_slug'], 'ama')
        self.assertEqual(self.db.store_accounts.find_one()['total_profit_balance'], 5)
        self.place(complaint_id)
        self.assertEqual(self.db.store_accounts.find_one()['total_profit_balance'], 5)

    def test_forwarded_store_report_uses_report_phone_and_normalizes_offer_label(self):
        complaint_id = str(self.db.complaints.insert_one({'user_id': self.owner_id, 'complaint_type': 'store_report',
            'source': 'store_page', 'store_slug': 'ama', 'store_name': 'Ama Store',
            'service_name': 'MTN Normal', 'offer': '5.0 GB', 'reporter_phone': '0240000000',
            'forwarded_to_admin': True, 'status': 'pending', 'submitted_at': datetime.utcnow()}).inserted_id)
        self.start(complaint_id)
        self.assertTrue(self.place(complaint_id).get_json()['success'])
        order = self.db.orders.find_one()
        self.assertEqual(order['store_slug'], 'ama')
        self.assertEqual(order['items'][0]['phone'], '0240000000')
        self.assertIsNone(order['user_id'])

    def test_unforwarded_reports_and_unauthorized_requests_cannot_place(self):
        complaint_id = str(self.db.complaints.insert_one({'complaint_type': 'store_report', 'store_slug': 'ama', 'forwarded_to_admin': False}).inserted_id)
        self.assertEqual(self.start(complaint_id).status_code, 404)
        complaint_id = self.complaint()
        self.assertEqual(self.client.post(f'/admin/complaints/order-recovery/{complaint_id}/0/start').status_code, 403)
        self.start(complaint_id)
        with self.client.session_transaction() as session:
            session['role'] = 'customer'
        self.assertEqual(self.place(complaint_id).status_code, 403)
        self.assertEqual(self.client.post('/checkout', json={'cart': self.cart, 'method': 'admin_override'}).status_code, 403)

    def test_unknown_origin_and_mismatched_linked_account_are_not_guessed(self):
        complaint_id = str(self.db.complaints.insert_one({'service_name': 'MTN Normal', 'offer': '5GB',
            'reporter_phone': '0240000000', 'status': 'pending', 'submitted_at': datetime.utcnow()}).inserted_id)
        response = self.start(complaint_id)
        self.assertEqual(self.client.get(response.location).status_code, 302)
        self.assertFalse(self.place(complaint_id).get_json()['success'])
        self.assertEqual(self.db.orders.count_documents({}), 0)
        complaint_id = self.complaint()
        self.db.complaints.update_one({'_id': ObjectId(complaint_id)}, {'$set': {'user_id': ObjectId()}})
        self.start(complaint_id)
        self.assertFalse(self.place(complaint_id).get_json()['success'])
        self.assertEqual(self.db.orders.count_documents({'admin_payment_override': {'$exists': True}}), 0)

    def test_normal_customer_checkout_still_deducts_wallet_and_requires_balance(self):
        with self.client.session_transaction() as session:
            session.update(role='customer', user_id=str(self.customer_id))
        self.assertEqual(self.client.post('/checkout', json={'cart': self.cart}).status_code, 400)
        self.db.balances.update_one({'user_id': self.customer_id}, {'$set': {'amount': 50}})
        response = self.client.post('/checkout', json={'cart': self.cart})
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(self.db.balances.find_one()['amount'], 30)
        self.assertEqual(self.db.transactions.find_one()['type'], 'purchase')
        self.assertNotIn('admin_payment_override', self.db.orders.find_one())

    def test_customer_complaint_still_calls_checkout_provider_worker(self):
        complaint_id = self.complaint()
        self.db.services.update_one({'_id': self.service_id}, {'$set': {'type': 'API', 'provider': 'bundleportal'}})
        self.start(complaint_id)
        with patch.object(self.worker, '_send_bundleportal_order', return_value=(True, {'success': True, 'reference': 'test-provider'})) as provider:
            self.assertTrue(self.place(complaint_id).get_json()['success'])
            provider.assert_called_once()
        self.assertEqual(self.db.balances.find_one()['amount'], 0)

    def test_complaints_list_displays_origin_recipient_action_and_replacement(self):
        complaint_id = self.complaint('index')
        html = self.client.get('/admin/complaints').get_data(as_text=True)
        self.assertIn('Place Order', html)
        self.assertIn('Origin: Index page', html)
        self.assertIn('0240000000', html)
        self.start(complaint_id)
        self.place(complaint_id)
        html = self.client.get('/admin/complaints').get_data(as_text=True)
        self.assertIn('Replacement order:', html)
        self.assertNotIn('>Place Order<', html)

    def test_placement_lock_and_provider_failure_stay_pending_without_duplicate_orders(self):
        complaint_id = self.complaint()
        self.start(complaint_id)
        self.db.complaints.update_one({'_id': ObjectId(complaint_id)}, {'$set': {'order_recovery.placement_state': 'placing'}})
        self.assertEqual(self.place(complaint_id).status_code, 409)
        self.client.post(f'/admin/complaints/{complaint_id}/update', data={'status': 'resolved'})
        self.assertEqual(self.db.complaints.find_one()['status'], 'pending')
        self.db.complaints.update_one({'_id': ObjectId(complaint_id)}, {'$unset': {'order_recovery.placement_state': ''}})
        self.db.services.update_one({'_id': self.service_id}, {'$set': {'type': 'API', 'provider': 'bundleportal'}})
        with patch.object(self.worker, '_send_bundleportal_order', return_value=(False, {'success': False, 'message': 'Provider declined'})) as provider:
            self.assertFalse(self.place(complaint_id).get_json()['success'])
            self.assertEqual(self.db.complaints.find_one()['status'], 'pending')
            self.assertFalse(self.place(complaint_id).get_json()['success'])
            provider.assert_called_once()
        self.assertEqual(self.db.orders.count_documents({'admin_payment_override.key': f'complaint:{complaint_id}:0'}), 1)

    def test_complaint_recovery_requires_complaints_permission(self):
        patch.object(self.access, 'db', self.db).start()
        self.access.init_admin_access(self.app)
        self.db.users.update_one({'_id': self.owner_id}, {'$set': {'managed_admin': True, 'allowed_pages': ['failed_receipts']}})
        complaint_id = self.complaint()
        response = self.client.post(f'/admin/complaints/order-recovery/{complaint_id}/0/start',
            json={'csrf_token': 'csrf'})
        self.assertEqual(response.status_code, 403)
        self.db.users.update_one({'_id': self.owner_id}, {'$set': {'allowed_pages': ['complaints']}})
        self.assertEqual(self.start(complaint_id).status_code, 302)
        self.assertTrue(self.place(complaint_id).get_json()['success'])


if __name__ == '__main__':
    unittest.main()
