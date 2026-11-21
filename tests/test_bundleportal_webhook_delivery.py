import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import test_bundle_portal_v2_mtn as existing_tests


class WebhookDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        existing_tests.BundlePortalMTNTests.setUpClass.__func__(cls)

    def setUp(self):
        existing_tests.BundlePortalMTNTests.setUp(self)
        self.environment = patch.dict(os.environ, {'BUNDLE_PORTAL_WEBHOOK_SECRET': 'whsec_test'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.dispatch = patch.object(self.callbacks, 'dispatch_webhook_event')
        self.dispatch_mock = self.dispatch.start()
        self.addCleanup(self.dispatch.stop)
        self.payload = {'event': 'order.completed', 'order_id': 'BP_REF', 'reference': 'KT-1',
            'status': 'completed', 'network': 'mtn', 'bundle': '5GB', 'recipient': '0241234567',
            'amount': 20, 'failure_reason': None, 'settled_at': '2026-10-11T20:00:00.000Z'}

    def order(self, provider='bundleportal_mtn'):
        self.db.orders.insert_one({'order_id': 'HAN1', 'status': 'processing', 'items': [{
            'provider': provider, 'provider_network': 'mtn', 'provider_request_order_id': 'BP_REF',
            'phone': '0241234567', 'line_status': 'processing', 'api_status': 'submitting'}]})

    def post(self, payload=None, delivery='delivery-1', signature=None, attempt=1):
        raw = json.dumps(payload or self.payload, separators=(',', ':')).encode()
        signature = signature or 'sha256=' + hmac.new(b'whsec_test', raw, hashlib.sha256).hexdigest()
        return self.client.post('/webhooks/bundleportal', data=raw, content_type='application/json',
            headers={'X-BundlePortal-Signature': signature, 'X-BundlePortal-Delivery': delivery,
                     'X-BundlePortal-Attempt': str(attempt)})

    def due(self):
        self.db.bundleportal_events.update_many({}, {'$set': {'next_attempt_at': datetime.utcnow() - timedelta(seconds=1)}})

    def test_acknowledges_durable_delivery_before_applying_and_deduplicates_id(self):
        self.order()
        self.assertEqual(self.post(attempt=2).status_code, 200)
        self.assertEqual(self.db.orders.find_one()['status'], 'processing')
        event = self.db.bundleportal_events.find_one()
        self.assertTrue(event['authenticated'])
        self.assertEqual(event['_id'], 'delivery:delivery-1')
        self.assertEqual(event['processing_state'], 'pending')
        self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['processed'], 1)
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.db.bundleportal_events.count_documents({}), 1)
        self.assertEqual(self.db.bundleportal_events.find_one()['delivery_attempt'], 2)
        self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['processed'], 0)

    def test_callback_before_order_is_saved_is_acknowledged_and_recovered(self):
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['awaiting_order'], 1)
        self.order()
        self.due()
        self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['processed'], 1)
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')

    def test_changed_payload_cannot_reuse_a_delivery_id(self):
        self.order()
        self.post()
        changed = dict(self.payload, event='order.failed', status='failed')
        self.assertEqual(self.post(changed).status_code, 409)
        self.assertEqual(self.db.bundleportal_events.count_documents({}), 1)
        self.callbacks.process_pending_webhook_events(self.db)
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')

    def test_invalid_signatures_and_details_never_change_an_order(self):
        self.order()
        self.assertEqual(self.post(signature='sha256=bad').status_code, 401)
        self.assertEqual(self.post(dict(self.payload, recipient='0551234567')).status_code, 400)
        self.assertEqual(self.post(dict(self.payload, network='mtn_2')).status_code, 400)
        self.assertEqual(self.post(dict(self.payload, settled_at='invalid')).status_code, 400)
        self.assertEqual(self.post(dict(self.payload, network={'invalid': True})).status_code, 400)
        self.assertEqual(self.db.bundleportal_events.count_documents({}), 0)
        self.assertEqual(self.db.orders.find_one()['status'], 'processing')

    def test_readiness_and_signed_probe_do_not_create_orders(self):
        info = self.client.get('/webhooks/bundleportal').get_json()
        self.assertTrue(info['configured'])
        self.assertNotIn('secret', info)
        self.assertTrue(self.post({'event': 'webhook.test'}).get_json()['test'])
        self.assertEqual(self.db.orders.count_documents({}), 0)
        self.assertEqual(self.db.bundleportal_events.count_documents({}), 0)
        with patch.dict(os.environ, {'BUNDLE_PORTAL_WEBHOOK_SECRET': '', 'BUNDLEPORTAL_WEBHOOK_SECRET': ''}):
            self.assertEqual(self.post().status_code, 503)

    def test_saved_delivery_recovers_parent_refresh_failure_and_expired_worker(self):
        self.order()
        self.post()
        with patch.object(self.callbacks, 'refresh_order', side_effect=RuntimeError('interrupted')), self.assertLogs(self.callbacks.logger, level='ERROR'):
            self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['retry'], 1)
        self.assertEqual(self.db.orders.find_one()['items'][0]['line_status'], 'delivered')
        self.due()
        self.db.bundleportal_events.update_one({}, {'$set': {'processing_state': 'processing',
            'lease_until': datetime.utcnow() - timedelta(seconds=1), 'claim_token': 'expired'}})
        self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['processed'], 1)
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')

    def test_legacy_orders_settle_from_webhook_and_are_not_polled(self):
        self.order('bundleportal')
        with patch.object(self.status.requests, 'post') as post, patch.object(self.status.requests, 'get') as get:
            self.status._run_order_status_sync()
        post.assert_not_called()
        get.assert_not_called()
        self.post()
        self.status._run_bundle_portal_status_sync()
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')

    def test_old_refund_cannot_overwrite_a_newer_completion(self):
        self.order()
        self.post()
        self.callbacks.process_pending_webhook_events(self.db)
        refund = dict(self.payload, event='order.refunded', status='refunded', settled_at='2026-10-11T19:00:00Z')
        self.post(refund, delivery='old-refund')
        self.callbacks.process_pending_webhook_events(self.db)
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')
        refund['settled_at'] = '2026-10-11T21:00:00Z'
        self.post(refund, delivery='new-refund')
        self.callbacks.process_pending_webhook_events(self.db)
        item = self.db.orders.find_one()['items'][0]
        self.assertEqual(item['provider_status'], 'refunded')
        self.assertTrue(item['refund_required'])

    def test_legacy_placement_reply_cannot_overwrite_a_fast_signed_completion(self):
        self.order('bundleportal')
        def submit(**kwargs):
            self.post()
            self.callbacks.process_pending_webhook_events(self.db)
            return True, {'success': True, 'data': {'status': 'processing', 'reference': 'KT-1'}}
        with patch.object(self.checkout, '_send_bundleportal_order', side_effect=submit):
            self.checkout._background_process_providers('HAN1', [{'provider': 'bundleportal',
                'provider_request_order_id': 'BP_REF', 'provider_network': 'mtn',
                'phone': '0241234567', 'package_size_gb': 5, 'amount': 20}])
        self.assertEqual(self.db.orders.find_one()['items'][0]['line_status'], 'delivered')

    def test_callback_never_bypasses_ussd_payment_verification(self):
        self.order()
        self.db.orders.update_one({}, {'$set': {'channel': 'arkesel_ussd', 'payment_status': 'pending', 'status': 'awaiting_payment'}})
        self.post()
        self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['awaiting_payment'], 1)
        self.assertEqual(self.db.orders.find_one()['status'], 'awaiting_payment')
        self.db.orders.update_one({}, {'$set': {'payment_status': 'paid'}})
        self.due()
        self.assertEqual(self.callbacks.process_pending_webhook_events(self.db)['processed'], 1)
        self.assertEqual(self.db.orders.find_one()['status'], 'delivered')

    def test_late_exception_cannot_overwrite_a_signed_legacy_completion(self):
        self.order('bundleportal')
        def submit(**kwargs):
            self.post()
            self.callbacks.process_pending_webhook_events(self.db)
            raise RuntimeError('reply interrupted')
        with patch.object(self.checkout, '_send_bundleportal_order', side_effect=submit):
            self.checkout._background_process_providers('HAN1', [{'provider': 'bundleportal',
                'provider_request_order_id': 'BP_REF', 'provider_network': 'mtn',
                'phone': '0241234567', 'package_size_gb': 5, 'amount': 20}])
        self.assertEqual(self.db.orders.find_one()['items'][0]['line_status'], 'delivered')


if __name__ == '__main__':
    unittest.main()
