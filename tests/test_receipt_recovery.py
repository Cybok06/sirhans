import unittest
import types
from unittest.mock import Mock, patch

import test_failed_receipts as public_tests


class ReceiptRecoveryTests(unittest.TestCase):
    catalog_token = public_tests.FailedReceiptTests.catalog_token

    def setUp(self):
        public_tests.FailedReceiptTests.setUp(self)
        self.recovery = self.module.receipt_recovery_module
        self.addCleanup(patch.stopall)
        patch.object(self.recovery, 'db', self.db).start()
        self.credit = patch.object(self.recovery, '_credit_recovery_store').start()
        self.client.post('/api/failed-receipts', json=self.payload)
        self.report_id = self.payload['submission_id']
        self.base = f'/admin/failed-receipts/{self.report_id}/0'
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
            session['user_id'] = 'admin-test-id'
        self.module_mock = types.SimpleNamespace(
            _reprice_public_cart=Mock(side_effect=lambda cart: (cart, 30)),
            _server_reprice_store_cart=Mock(side_effect=lambda store, cart: (cart, 30)),
            render_index_page=Mock(return_value='Index source'),
            render_store_page=Mock(return_value='Store source'),
        )
        self.source = patch.object(self.recovery, '_source_context', return_value=(self.module_mock, None, self.services)).start()

    def entry(self):
        return self.db.failed_receipts.find_one({'_id': self.report_id})['entries'][0]

    def start(self):
        return self.client.post(self.base + '/start', data={'csrf_token': 'test-token'})

    def place(self):
        with self.client.session_transaction() as session:
            ticket = session['receipt_recovery_tickets'][self.report_id + ':0']
        return self.client.post(self.base + '/place', json={'csrf_token': 'test-token', 'token': ticket['token'],
                                                          'cart': [{'phone': 'forged', 'amount': 0}]})

    def place_order(self, module, store, cart, override):
        self.db.orders.insert_one({'order_id': 'RECOVERY-1', 'status': 'processing',
            'items': [{'line_status': 'processing', 'api_status': 'manual_pending'}],
            'admin_payment_override': override, 'payment_status': 'admin_override',
            'store_slug': store['slug'] if store else None})
        return {'success': True, 'order_id': 'RECOVERY-1'}, 200

    def test_statuses_are_per_entry_and_filter_counts_include_legacy_reports(self):
        response = self.client.post(self.base + '/status', data={'csrf_token': 'test-token', 'status': 'resolved'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.entry()['status'], 'resolved')
        report = self.db.failed_receipts.find_one()
        self.assertEqual(report['entries'][1]['status'], 'unresolved')
        html = self.client.get('/admin/failed-receipts?status=resolved').get_data(as_text=True)
        self.assertIn('Resolved (1)', html)
        self.assertIn('Unresolved (1)', html)
        self.assertIn('0240000000', html)
        self.assertNotIn('data-phone="0200000000"', html)
        self.db.failed_receipts.update_one({'_id': self.report_id}, {'$unset': {'entries.1.status': ''}})
        html = self.client.get('/admin/failed-receipts?status=unresolved').get_data(as_text=True)
        self.assertIn('data-phone="0200000000"', html)

    def test_place_opens_source_loads_saved_cart_and_resolves_only_selected_entry(self):
        response = self.start()
        self.assertTrue(response.location.endswith('/source'))
        self.assertEqual(self.client.get(response.location).status_code, 200)
        config = self.module_mock.render_index_page.call_args.kwargs['receipt_recovery']
        self.assertEqual(config['cart'][0]['phone'], '0240000000')
        with patch.object(self.recovery, '_checkout', side_effect=self.place_order) as checkout:
            response = self.place()
            self.assertTrue(response.get_json()['success'])
            self.assertEqual(checkout.call_args.args[2][0]['phone'], '0240000000')
            self.assertEqual(checkout.call_args.args[3]['admin_id'], 'admin-test-id')
            self.assertEqual(self.entry()['status'], 'resolved')
            self.assertEqual(self.entry()['order_id'], 'RECOVERY-1')
            self.assertEqual(self.db.failed_receipts.find_one()['entries'][1]['status'], 'unresolved')
            self.assertTrue(self.place().get_json()['success'])
            self.assertEqual(checkout.call_count, 1)
            self.assertEqual(self.db.orders.count_documents({}), 1)
            self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
            self.assertEqual(self.entry()['status'], 'resolved')

    def test_store_uses_original_store_and_credit_recovery(self):
        self.db.failed_receipts.update_one({'_id': self.report_id}, {'$set': {'source': 'store', 'store_slug': 'ama'}})
        store = {'slug': 'ama'}
        self.source.return_value = self.module_mock, store, self.services
        response = self.start()
        self.client.get(response.location)
        self.module_mock.render_store_page.assert_called_once()
        with patch.object(self.recovery, '_checkout', side_effect=self.place_order) as checkout:
            self.assertTrue(self.place().get_json()['success'])
            self.assertEqual(checkout.call_args.args[1], store)
            self.credit.assert_called_once()
            self.assertEqual(self.credit.call_args.args[0], 'ama')

    def test_csrf_ticket_and_role_are_required(self):
        self.assertEqual(self.client.get(self.base + '/source').status_code, 403)
        self.assertEqual(self.client.post(self.base + '/start', data={}).status_code, 403)
        self.assertEqual(self.client.post(self.base + '/status', data={'status': 'resolved'}).status_code, 403)
        self.assertEqual(self.client.post(self.base + '/place', json=['invalid']).status_code, 403)
        self.start()
        self.assertEqual(self.client.post(self.base + '/place', json={'csrf_token': 'test-token', 'token': 'fake'}).status_code, 403)
        with self.client.session_transaction() as session:
            session['role'] = 'customer'
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(self.place().status_code, 403)
        self.assertEqual(self.db.orders.count_documents({}), 0)

    def test_preflight_failure_can_retry_but_interrupted_side_effects_cannot(self):
        self.start()
        self.module_mock._reprice_public_cart.side_effect = ValueError('Service closed')
        with patch.object(self.recovery, '_checkout') as checkout:
            self.assertEqual(self.place().status_code, 400)
            checkout.assert_not_called()
            self.assertNotIn('placement_state', self.entry())
        self.module_mock._reprice_public_cart.side_effect = lambda cart: (cart, 30)
        with patch.object(self.recovery, '_checkout', side_effect=RuntimeError('connection interrupted')) as checkout, self.assertLogs(self.app.logger, level='ERROR'):
            self.assertEqual(self.place().status_code, 409)
            self.assertEqual(self.entry()['status'], 'unresolved')
            self.assertEqual(self.entry()['placement_state'], 'attention')
            self.assertEqual(self.place().status_code, 409)
            self.assertEqual(checkout.call_count, 1)

    def test_existing_failed_provider_order_stays_unresolved_and_is_not_resubmitted(self):
        self.start()
        def failed(module, store, cart, override):
            self.place_order(module, store, cart, override)
            self.db.orders.update_one({'order_id': 'RECOVERY-1'}, {'$set': {'status': 'failed'}})
            return {'success': True}, 200
        with patch.object(self.recovery, '_checkout', side_effect=failed) as checkout:
            self.assertFalse(self.place().get_json()['success'])
            self.assertEqual(self.entry()['status'], 'unresolved')
            self.assertEqual(self.entry()['order_id'], 'RECOVERY-1')
            self.assertFalse(self.place().get_json()['success'])
            self.assertEqual(checkout.call_count, 1)

    def test_lock_blocks_double_click_and_manual_status_change(self):
        self.start()
        self.db.failed_receipts.update_one({'_id': self.report_id}, {'$set': {'entries.0.placement_state': 'placing'}})
        with patch.object(self.recovery, '_checkout') as checkout:
            self.assertEqual(self.place().status_code, 409)
            checkout.assert_not_called()
        self.client.post(self.base + '/status', data={'csrf_token': 'test-token', 'status': 'resolved'})
        self.assertEqual(self.entry()['status'], 'unresolved')

    def test_offer_reordering_matches_label_and_ambiguous_or_missing_offers_fail(self):
        self.services[0]['offers'].reverse()
        cart = self.recovery._build_cart(self.entry(), self.services)
        self.assertEqual(cart[0]['amount'], 30)
        self.services[0]['offers'].append({'value_text': '5GB', 'total': 40})
        with self.assertRaises(ValueError):
            self.recovery._build_cart(self.entry(), self.services)
        with self.assertRaises(ValueError):
            self.recovery._build_cart({'phone': '0240000000', 'offer': '5GB'}, self.services)

    def test_dispatch_uses_shared_helpers_without_payment_verification(self):
        cart = [{'phone': '0240000000'}]
        override = {'key': self.report_id + ':0', 'customer_id': None}
        index = types.SimpleNamespace(_reprice_public_cart=Mock(return_value=(cart, 30)),
                                      _create_public_order_from_verified_payment=Mock(return_value={'success': True}))
        self.recovery._checkout(index, None, cart, override)
        call = index._create_public_order_from_verified_payment.call_args
        self.assertEqual(call.args[1], {})
        self.assertEqual(call.kwargs['payment_status'], 'admin_override')
        self.assertEqual(call.kwargs['admin_override'], override)
        store = types.SimpleNamespace(place_store_order=Mock(return_value=({'success': True}, 200)))
        self.recovery._checkout(store, {'slug': 'ama'}, cart, override)
        payload = store.place_store_order.call_args.args[0]
        self.assertNotIn('paystack_reference', payload)
        self.assertEqual(payload['payment_status'], 'admin_override')
        self.assertEqual(store.place_store_order.call_args.kwargs['channel'], 'store_checkout')


if __name__ == '__main__':
    unittest.main()
