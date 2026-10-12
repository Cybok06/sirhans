"""Isolated v2 tests: no live database, payments, or provider purchases."""
import hashlib
import hmac
import importlib
import json
import os
import sys
import types
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import mongomock
from bson import ObjectId
from flask import Flask


class BundlePortalMTNTests(unittest.TestCase):
    def test_legacy_ishare_request_uses_documented_v2_endpoint(self):
        response = Mock(status_code=200, text='')
        response.json.return_value = {'success': True, 'data': {'status': 'processing'}}
        with patch.object(self.checkout, 'BUNDLEPORTAL_API_KEY', 'test-key'), patch.object(
            self.checkout.requests, 'post', return_value=response
        ) as post:
            ok, _ = self.checkout._send_bundleportal_order(
                '0271234567', 'airteltigo', 3, 'SAME-LINE-REF', 'TEST-ORDER', [])
        self.assertTrue(ok)
        self.assertEqual('https://api.bundleportal.com/v2', post.call_args.args[0])
        self.assertEqual({'action': 'place_order', 'network': 'airteltigo',
            'recipient': '0271234567', 'package_size': 3, 'order_id': 'SAME-LINE-REF'},
            post.call_args.kwargs['json'])

    def test_legacy_ishare_preserves_fractional_bundle_sizes(self):
        response = Mock(status_code=200, text='')
        response.json.return_value = {'success': True}
        with patch.object(self.checkout, 'BUNDLEPORTAL_API_KEY', 'test-key'), patch.object(
            self.checkout.requests, 'post', return_value=response
        ) as post:
            self.checkout._send_bundleportal_order(
                '0271234567', 'ishare', 0.5, 'SAME-LINE-REF', 'TEST-ORDER', [])
        self.assertEqual(0.5, post.call_args.kwargs['json']['package_size'])
        self.assertEqual('airteltigo', post.call_args.kwargs['json']['network'])

    @classmethod
    def setUpClass(cls):
        from mongomock.gridfs import enable_gridfs_integration
        enable_gridfs_integration()
        cls.db = mongomock.MongoClient().sirhans
        fake = types.ModuleType('db')
        fake.db = cls.db
        cls.isolation = patch.dict(sys.modules, {'db': fake})
        cls.isolation.start()
        cls.addClassCleanup(cls.isolation.stop)
        with patch('apscheduler.schedulers.background.BackgroundScheduler.start'):
            cls.api = importlib.import_module('bundle_portal')
            cls.callbacks = importlib.import_module('bundle_portal_orders')
            cls.checkout = importlib.import_module('checkout')
            cls.store = importlib.import_module('routes.store_page')
            cls.public = importlib.import_module('index')
            cls.services = importlib.import_module('admin_services')
            cls.status = importlib.import_module('order_status')

    def setUp(self):
        batch_save = patch.object(self.store, '_save_store_order_batch',
                                  side_effect=lambda docs: self.store.orders_col.insert_many(docs))
        batch_save.start()
        self.addCleanup(batch_save.stop)
        for name in self.db.list_collection_names():
            self.db[name].delete_many({})
        self.user = ObjectId()
        self.db.balances.insert_one({'user_id': self.user, 'amount': 1000})
        self.db.settings.insert_one({'_id': 'checkout_controls', 'enforce_known_number_check': False})
        self.db.stores.insert_one({'slug': 'shop', 'owner_id': self.user})
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        self.app.config['BUNDLEPORTAL_WEBHOOK_ASYNC'] = False
        for bp in (self.checkout.checkout_bp, self.store.stores_bp,
                   self.services.admin_services_bp, self.callbacks.bundle_portal_bp):
            self.app.register_blueprint(bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=str(self.user), role='customer')

    def service_cart(self, name, provider, service_type='API'):
        sid = self.db.services.insert_one({'name': name, 'provider': provider, 'type': service_type,
            'status': 'OPEN', 'availability': 'AVAILABLE', 'offers': [], 'network': 'mtn'}).inserted_id
        return [{'serviceId': str(sid), 'serviceName': name, 'phone': '0241234567',
                 'amount': 20, 'base_amount': 18, 'value': '5GB', 'value_obj': {'volume': 5000}}]

    def test_all_three_channels_keep_selected_route_for_both_mtn_services(self):
        for name in ('MTN NORMAL', 'MTN EXPRESS', 'AT iShare', 'Telecel', 'Vodafone'):
            for provider, network in self.api.PROVIDERS.items():
                if not self.api.supports_service(provider, {'name': name}):
                    continue
                for channel in ('customer', 'store', 'index'):
                    with self.subTest(service=name, provider=provider, channel=channel):
                        self.db.orders.delete_many({})
                        cart = self.service_cart(name, provider)
                        with patch('threading.Thread'), patch.object(self.checkout, '_known_number_validation_error', return_value=None):
                            if channel == 'customer':
                                response = self.client.post('/checkout', json={'cart': cart})
                                self.assertEqual(response.status_code, 200, response.json)
                            elif channel == 'store':
                                with patch.object(self.store, '_server_reprice_store_cart', return_value=(cart, 20)), patch.object(
                                        self.store, '_verify_paystack', return_value=(True, {'amount': 2000, 'currency': 'GHS'}, 'OK')), patch.object(
                                        self.store, '_background_process_providers'):
                                    response = self.client.post('/store-checkout/shop', json={
                                        'cart': cart, 'method': 'paystack_inline', 'paystack': {'reference': str(ObjectId())}})
                                self.assertEqual(response.status_code, 200, response.json)
                            else:
                                with self.app.test_request_context('/'):
                                    result = self.public._create_public_order_from_verified_payment(
                                        str(ObjectId()), {'amount': 2000, 'currency': 'GHS'}, cart, 20,
                                        defer_provider_processing=True)
                                self.assertTrue(result['success'], result)
                        order = self.db.orders.find_one({})
                        self.assertIsNotNone(order)
                        item = order['items'][0]
                        self.assertEqual(item['provider'], provider)
                        self.assertEqual(item['provider_network'], network)
                        self.assertEqual(item['package_size_gb'], 5)
                        self.assertTrue(item['provider_request_order_id'])

    def test_worker_uses_v2_preflights_and_exact_route_once(self):
        for provider, network in self.api.PROVIDERS.items():
            self.db.orders.delete_many({})
            reference = provider + '_ref'
            self.db.orders.insert_one({'order_id': 'HAN1', 'status': 'processing', 'items': [{
                'provider': provider, 'provider_request_order_id': reference,
                'phone': '0241234567', 'line_status': 'processing', 'api_status': 'submitting'}]})
            job = {'provider': provider, 'provider_request_order_id': reference,
                   'phone': '0241234567', 'package_size_gb': 5, 'amount': 20}
            with patch.object(self.api, 'call', side_effect=[
                {'success': True, 'data': {'bundles': [{'network': network, 'size_gb': 5}]}},
                {'success': True, 'data': {'can_order': True}},
                {'success': True, 'data': {'status': 'cached', 'reference': 'KT1', 'amount': 15}},
            ]) as call:
                self.checkout._background_process_providers('HAN1', [job])
                self.checkout._background_process_providers('HAN1', [job])
            self.assertEqual([c.args[0] for c in call.call_args_list], ['get_bundles', 'verify_number', 'place_order'])
            self.assertTrue(all(c.kwargs['network'] == network for c in call.call_args_list))
            self.assertEqual(call.call_args.kwargs['order_id'], reference)
            order = self.db.orders.find_one({})
            self.assertEqual(order['items'][0]['line_status'], 'processing')
            self.assertEqual(order['profit_amount_total'], 5)

    def test_admin_selects_routes_only_for_mtn_services(self):
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
        with patch.object(self.services, 'can_manage_providers', return_value=True), patch.dict(os.environ, {
                'BUNDLE_PORTAL_KEY': 'test', 'BUNDLE_PORTAL_WEBHOOK_SECRET': 'secret'}):
            for name in ('MTN NORMAL', 'MTN EXPRESS', 'Telecel', 'Vodafone', 'AT iShare', 'AT Bigtime'):
                cart = self.service_cart(name, 'portal02')
                for provider in self.api.PROVIDERS:
                    response = self.client.post('/admin/services/' + cart[0]['serviceId'] + '/provider', json={'provider': provider})
                    expected = 200 if self.api.supports_service(provider, {'name': name}) else 400
                    self.assertEqual(response.status_code, expected)

    def test_webhook_signature_and_final_status(self):
        reference = 'HAN_BP_ref'
        self.db.orders.insert_one({'order_id': 'HAN1', 'status': 'processing', 'items': [{
            'provider': 'bundleportal_mtn3', 'provider_request_order_id': reference,
            'phone': '0241234567', 'line_status': 'processing'}]})
        raw = json.dumps({'event': 'order.completed', 'status': 'completed', 'order_id': reference,
                          'network': 'mtn_3', 'recipient': '0241234567'}).encode()
        signature = 'sha256=' + hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
        with patch.dict(os.environ, {'BUNDLE_PORTAL_WEBHOOK_SECRET': 'secret'}):
            self.assertEqual(self.client.post('/webhooks/bundleportal', data=raw, content_type='application/json').status_code, 401)
            for _ in range(2):
                response = self.client.post('/webhooks/bundleportal', data=raw, content_type='application/json',
                                            headers={'X-BundlePortal-Signature': signature})
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.orders.find_one({})['status'], 'delivered')
        self.assertEqual(self.db.bundleportal_events.count_documents({}), 1)

    def test_v2_orders_are_not_polled(self):
        for provider in self.api.PROVIDERS:
            self.db.orders.insert_one({'order_id': provider, 'status': 'processing', 'items': [{
                'provider': provider, 'line_status': 'processing', 'provider_order_id': 'ref'}]})
        with patch.object(self.status.requests, 'post') as post, patch.object(self.status.requests, 'get') as get:
            self.status._run_order_status_sync()
        post.assert_not_called()
        get.assert_not_called()

    def test_off_service_stays_manual(self):
        cart = self.service_cart('MTN NORMAL', 'bundleportal_mtn2', 'OFF')
        with patch('threading.Thread'), patch.object(self.checkout, '_known_number_validation_error', return_value=None):
            response = self.client.post('/checkout', json={'cart': cart})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.db.orders.find_one({})['items'][0].get('provider'), self.api.PROVIDERS)

    def test_timeout_stays_for_review_without_resubmitting_purchase(self):
        order = {'order_id': 'HAN1', 'status': 'processing', 'items': [{
            'provider': 'bundleportal_mtn', 'provider_request_order_id': 'ref',
            'line_status': 'processing', 'api_status': 'submitting'}]}
        self.db.orders.insert_one(order)
        with patch.object(self.callbacks, 'submit', return_value={'success': False, 'code': 'unknown_outcome'}):
            self.callbacks.process_job(self.db.orders, 'HAN1', {'provider': 'bundleportal_mtn',
                'provider_request_order_id': 'ref', 'phone': '0241234567', 'package_size_gb': 5})
        item = self.db.orders.find_one({})['items'][0]
        self.assertEqual((item['line_status'], item['api_status']), ('processing', 'review_required'))

    def test_sizes_preserve_fractional_and_larger_bundles(self):
        self.assertEqual(self.api.package_size({'volume': 1500}, {}), 1.5)
        self.assertEqual(self.api.package_size({'volume': 12000}, {}), 12)
        cart = self.service_cart('MTN NORMAL', 'bundleportal_mtn3')
        cart[0].update(value='1.5GB', value_obj={'volume': 1500})
        with patch('threading.Thread'), patch.object(self.checkout, '_known_number_validation_error', return_value=None):
            response = self.client.post('/checkout', json={'cart': cart})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.orders.find_one({})['items'][0]['package_size_gb'], 1.5)

    def test_webhook_arriving_during_submission_is_not_overwritten(self):
        self.db.orders.insert_one({'order_id': 'HAN1', 'status': 'processing', 'items': [{
            'provider': 'bundleportal_mtn', 'provider_request_order_id': 'ref',
            'phone': '0241234567', 'line_status': 'processing', 'api_status': 'submitting'}]})
        def submit(*args, **kwargs):
            order = self.db.orders.find_one({})
            self.callbacks.apply_status_event(self.db.orders, order, order['items'][0], {
                'order_id': 'ref', 'event': 'order.completed', 'status': 'completed',
                'network': 'mtn', 'recipient': '0241234567'})
            return {'success': True, 'data': {'status': 'processing'}}
        with patch.object(self.callbacks, 'submit', side_effect=submit):
            self.callbacks.process_job(self.db.orders, 'HAN1', {'provider': 'bundleportal_mtn',
                'provider_request_order_id': 'ref', 'phone': '0241234567', 'package_size_gb': 5})
        self.assertEqual(self.db.orders.find_one({})['items'][0]['line_status'], 'delivered')

    def test_api_uses_v2_endpoint_and_existing_key_alias(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {'success': True, 'data': {'can_order': True}}
        with patch.dict(os.environ, {'BUNDLE_PORTAL_KEY': '', 'BUNDLEPORTAL_API_KEY': 'test-key'}), patch.object(
                self.api.requests, 'post', return_value=response) as post:
            self.api.call('verify_number', network='mtn_2', recipient='0241234567')
        self.assertEqual(post.call_args.args[0], 'https://api.bundleportal.com/v2')
        self.assertEqual(post.call_args.kwargs['headers']['x-api-key'], 'test-key')
        self.assertEqual(post.call_args.kwargs['json'], {
            'action': 'verify_number', 'network': 'mtn_2', 'recipient': '0241234567'})

    def test_ishare_alias_webhooks_apply_completion_and_failure(self):
        for status in ('completed', 'failed', 'cancelled', 'refunded'):
            with self.subTest(status=status):
                self.db.orders.delete_many({})
                self.db.orders.insert_one({'order_id': 'ISHARE1', 'status': 'processing', 'items': [{
                    'provider': 'bundleportal_ishare', 'provider_request_order_id': 'ishare_ref',
                    'phone': '0271234567', 'line_status': 'processing'}]})
                raw = json.dumps({'order_id': 'ishare_ref', 'event': 'order.' + status,
                    'status': status, 'network': 'ishare', 'recipient': '0271234567'}).encode()
                signature = 'sha256=' + hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
                with patch.dict(os.environ, {'BUNDLE_PORTAL_WEBHOOK_SECRET': 'secret'}):
                    response = self.client.post('/webhooks/bundleportal', data=raw, content_type='application/json',
                        headers={'X-BundlePortal-Signature': signature})
                self.assertEqual(response.status_code, 200)
                item = self.db.orders.find_one({})['items'][0]
                self.assertEqual(item['line_status'], 'delivered' if status == 'completed' else 'failed')
                self.assertEqual(item['refund_required'], status != 'completed')

    def test_ishare_service_names_and_wrong_product_rejection(self):
        for name in ('AT iShare', 'AT - iShare', 'AirtelTigo iShare', 'iShare'):
            self.assertTrue(self.api.supports_service('bundleportal_ishare', {'name': name}))
        for name in ('AT Bigtime', 'MTN NORMAL', 'MTN EXPRESS', 'Telecel'):
            self.assertFalse(self.api.supports_service('bundleportal_ishare', {'name': name}))

    def test_telecel_service_names_and_wrong_product_rejection(self):
        for service in ({'name': 'Telecel'}, {'name': 'Vodafone'},
                        {'name': 'Data', 'network': 'telecel'},
                        {'name': 'Data', 'service_network': 'vodafone'}):
            self.assertTrue(self.api.supports_service('bundleportal_telecel', service))
        for name in ('MTN NORMAL', 'MTN EXPRESS', 'AT iShare', 'AT Bigtime'):
            self.assertFalse(self.api.supports_service('bundleportal_telecel', {'name': name}))

    def test_telecel_webhooks_update_status_and_reject_wrong_network(self):
        for status in ('completed', 'failed', 'cancelled', 'refunded'):
            with self.subTest(status=status):
                self.db.orders.delete_many({})
                self.db.orders.insert_one({'order_id': 'TELECEL1', 'status': 'processing', 'items': [{
                    'provider': 'bundleportal_telecel', 'provider_request_order_id': 'telecel_ref',
                    'phone': '0201234567', 'line_status': 'processing'}]})
                for network in ('mtn', 'telecel'):
                    raw = json.dumps({'order_id': 'telecel_ref', 'event': 'order.' + status,
                        'status': status, 'network': network, 'recipient': '0201234567'}).encode()
                    signature = 'sha256=' + hmac.new(b'secret', raw, hashlib.sha256).hexdigest()
                    with patch.dict(os.environ, {'BUNDLE_PORTAL_WEBHOOK_SECRET': 'secret'}):
                        response = self.client.post('/webhooks/bundleportal', data=raw, content_type='application/json',
                            headers={'X-BundlePortal-Signature': signature})
                    self.assertEqual(response.status_code, 200 if network == 'telecel' else 400)
                item = self.db.orders.find_one({})['items'][0]
                self.assertEqual(item['line_status'], 'delivered' if status == 'completed' else 'failed')
                self.assertEqual(item['refund_required'], status != 'completed')

    def test_telecel_off_service_stays_manual(self):
        cart = self.service_cart('Telecel', 'bundleportal_telecel', 'OFF')
        with patch('threading.Thread'), patch.object(self.checkout, '_known_number_validation_error', return_value=None):
            response = self.client.post('/checkout', json={'cart': cart})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.db.orders.find_one({})['items'][0].get('provider'), self.api.PROVIDERS)

    def test_reconciliation_applies_saved_results_for_every_v2_route(self):
        from datetime import datetime
        for provider, network in self.api.PROVIDERS.items():
            for status in ('completed', 'failed', 'cancelled', 'refunded'):
                reference = provider + '_' + status
                self.db.orders.insert_one({'order_id': reference, 'status': 'processing', 'items': [{
                    'provider': provider, 'provider_request_order_id': reference,
                    'phone': '0241234567', 'line_status': 'processing'}]})
                self.db.bundleportal_events.insert_one({'received_at': datetime.now(), 'payload': {
                    'order_id': reference, 'event': 'order.' + status, 'status': status,
                    'network': network, 'recipient': '0241234567'}})
        with patch.object(self.status.requests, 'post') as post, patch.object(self.status.requests, 'get') as get:
            result = self.status._run_order_status_sync()
        self.assertEqual(result['updated_lines'], len(self.api.PROVIDERS) * 4)
        self.assertEqual(result['completed_lines'], len(self.api.PROVIDERS))
        for order in self.db.orders.find({}):
            self.assertEqual(order['status'], order['items'][0]['line_status'])
        post.assert_not_called()
        get.assert_not_called()
        with patch.object(self.db.bundleportal_events, 'find_one') as lookup:
            self.assertEqual(self.status._run_bundle_portal_status_sync()['checked_lines'], 0)
            lookup.assert_not_called()

    def test_reconciliation_keeps_cached_orders_waiting_for_webhook(self):
        self.db.orders.insert_one({'order_id': 'WAIT', 'status': 'processing', 'items': [{
            'provider': 'bundleportal_mtn', 'provider_request_order_id': 'waiting',
            'phone': '0241234567', 'line_status': 'cached'}]})
        result = self.status._run_bundle_portal_status_sync()
        self.assertEqual(result['awaiting_webhook'], 1)
        self.assertEqual(self.db.orders.find_one({})['items'][0]['line_status'], 'cached')

    def test_reconciliation_skips_unpaid_ussd(self):
        from datetime import datetime
        self.db.orders.insert_one({'order_id': 'WAIT', 'status': 'awaiting_payment',
            'channel': 'arkesel_ussd', 'payment_status': 'pending', 'items': [{
                'provider': 'bundleportal_mtn', 'provider_request_order_id': 'waiting',
                'phone': '0241234567', 'line_status': 'processing'}]})
        self.db.bundleportal_events.insert_one({'received_at': datetime.now(), 'payload': {
            'order_id': 'waiting', 'event': 'order.completed', 'status': 'completed',
            'network': 'mtn', 'recipient': '0241234567'}})
        self.assertEqual(self.status._run_bundle_portal_status_sync()['checked_lines'], 0)
        self.assertEqual(self.db.orders.find_one({})['status'], 'awaiting_payment')

    def test_reconciliation_retries_failed_parent_refresh_without_rechecking_final_line(self):
        from datetime import datetime
        self.db.orders.insert_one({'order_id': 'HAN1', 'status': 'processing', 'items': [{
            'provider': 'bundleportal_mtn', 'provider_request_order_id': 'ref',
            'phone': '0241234567', 'line_status': 'processing'}]})
        self.db.bundleportal_events.insert_one({'received_at': datetime.now(), 'payload': {
            'order_id': 'ref', 'event': 'order.completed', 'status': 'completed',
            'network': 'mtn', 'recipient': '0241234567'}})
        with patch.object(self.callbacks, 'refresh_order', side_effect=RuntimeError('Temporary failure')):
            result = self.status._run_bundle_portal_status_sync()
        self.assertEqual(result['errors'], 1)
        self.assertTrue(self.db.orders.find_one({})['bundleportal_parent_refresh_pending'])
        with patch.object(self.db.bundleportal_events, 'find_one') as lookup:
            result = self.status._run_bundle_portal_status_sync()
            lookup.assert_not_called()
        self.assertEqual(result['updated_orders'], 1)
        self.assertEqual(self.db.orders.find_one({})['status'], 'delivered')
        self.assertFalse(self.db.orders.find_one({})['bundleportal_parent_refresh_pending'])

    def test_reconciliation_rejects_mismatched_event_and_continues_after_lookup_error(self):
        from datetime import datetime
        for reference in ('bad', 'good'):
            self.db.orders.insert_one({'order_id': reference, 'status': 'processing', 'items': [{
                'provider': 'bundleportal_mtn', 'provider_request_order_id': reference,
                'phone': '0241234567', 'line_status': 'processing'}]})
            self.db.bundleportal_events.insert_one({'received_at': datetime.now(), 'payload': {
                'order_id': reference, 'event': 'order.completed', 'status': 'completed',
                'network': 'mtn', 'recipient': '0551234567' if reference == 'bad' else '0241234567'}})
        original = self.db.bundleportal_events.find_one
        def lookup(query, **kwargs):
            if query['payload.order_id'] == 'bad':
                raise RuntimeError('Temporary failure')
            return original(query, **kwargs)
        with patch.object(self.db.bundleportal_events, 'find_one', side_effect=lookup):
            result = self.status._run_bundle_portal_status_sync()
        self.assertEqual((result['errors'], result['updated_lines']), (1, 1))
        result = self.status._run_bundle_portal_status_sync()
        self.assertEqual(result['ignored_events'], 1)
        self.assertEqual(self.db.orders.find_one({'order_id': 'bad'})['status'], 'processing')

    def test_status_scheduler_runs_every_three_minutes(self):
        job = self.status.scheduler.get_job('order_status_sync')
        self.assertEqual(job.trigger.interval.total_seconds(), 180)
        self.assertEqual(job.max_instances, 1)


if __name__ == '__main__':
    unittest.main()
