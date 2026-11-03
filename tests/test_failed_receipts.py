import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from uuid import uuid4

import mongomock
from bson import ObjectId
from flask import Flask, render_template

ROOT = Path(__file__).resolve().parents[1]


class FailedReceiptTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test
        with patch.dict(sys.modules, {'db': types.SimpleNamespace(db=self.db)}):
            spec = importlib.util.spec_from_file_location('receipt_test_module', ROOT / 'failed_receipts.py')
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        self.app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='test')
        self.app.register_blueprint(self.module.failed_receipts_bp)
        self.app.add_url_rule('/login', 'login.login', lambda: 'login')
        self.app.add_url_rule('/logout', 'login.logout', lambda: 'logout')
        self.app.context_processor(lambda: {'can_access_page': lambda key: key == 'failed_receipts', 'admin_page_catalog': []})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['receipt_csrf'] = 'test-token'
        self.services = [{'_id_str': 'mtn', 'name': 'MTN', 'image_url': 'https://example.com/mtn.png',
                          'offers': [{'value_text': '5GB', 'total': 30}, {'value_text': '2GB', 'total': 15}]}]
        self.payload = {'csrf_token': 'test-token', 'source': 'index', 'submission_id': str(uuid4()),
                        'catalog_token': self.catalog_token(),
                        'entries': [{'phone': '0240000000', 'service_id': 'mtn', 'offer_index': 0},
                                    {'phone': '+233200000000', 'service_id': 'mtn', 'offer_index': 1}]}

    def catalog_token(self, source='index', slug=''):
        with self.app.app_context():
            return self.module.receipt_catalog(self.services, source, slug)['token']

    def test_batch_and_retry_are_counted_once(self):
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.assertEqual(self.db.failed_receipts.count_documents({}), 1)
        record = self.db.failed_receipts.find_one()
        self.assertEqual(record['entry_count'], 2)
        self.assertEqual(record['entries'][1]['phone'], '0200000000')
        self.assertEqual(record['source'], 'index')

    def test_validation_is_atomic_and_csrf_required(self):
        self.payload['entries'][1]['phone'] = 'invalid'
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 400)
        self.assertEqual(self.db.failed_receipts.count_documents({}), 0)
        self.payload['csrf_token'] = 'wrong'
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 403)
        self.assertEqual(self.client.post('/api/failed-receipts', json=[]).status_code, 400)

    def test_report_type_is_saved_validated_and_part_of_retry_identity(self):
        self.payload['report_type'] = 'invalid'
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 400)
        self.assertEqual(self.db.failed_receipts.count_documents({}), 0)
        self.payload['report_type'] = 'delivery_issue'
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.assertEqual(self.db.failed_receipts.find_one()['report_type'], 'delivery_issue')
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.payload['report_type'] = 'no_receipt'
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 409)

    def test_admin_type_filters_include_legacy_and_combine_with_status(self):
        self.client.post('/api/failed-receipts', json=self.payload)
        self.db.failed_receipts.update_one({}, {'$unset': {'report_type': ''}})
        self.payload.update(submission_id=str(uuid4()), report_type='delivery_issue')
        self.payload['entries'] = [{'phone': '0551234567', 'service_id': 'mtn', 'offer_index': 0}]
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.db.failed_receipts.update_one({'report_type': 'delivery_issue'}, {'$set': {'entries.0.status': 'resolved'}})
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
        red = self.client.get('/admin/failed-receipts?report_type=no_receipt').get_data(as_text=True)
        self.assertIn('data-phone="0240000000"', red)
        self.assertNotIn('data-phone="0551234567"', red)
        green = self.client.get('/admin/failed-receipts?report_type=delivery_issue&status=resolved').get_data(as_text=True)
        self.assertIn('data-phone="0551234567"', green)
        self.assertNotIn('data-phone="0240000000"', green)
        self.assertIn('Resolved (1)', green)
        self.assertIn('report_type=delivery_issue', green)
        empty = self.client.get('/admin/failed-receipts?report_type=delivery_issue&status=unresolved').get_data(as_text=True)
        self.assertNotIn('data-phone=', empty)

    def test_both_pages_include_shared_reporting_guide(self):
        for page in ('index.html', 'store_page.html'):
            self.assertIn("{% include 'order_report_actions.html' %}", (ROOT / 'templates' / page).read_text(encoding='utf-8'))
        with self.app.test_request_context('/'):
            html = render_template('order_report_actions.html', report_store_slug='ama store')
        self.assertIn('HOW TO REPORT YOUR ORDER', html)
        self.assertIn('after 24 hours', html)
        self.assertIn('data-report-type="no_receipt"', html)
        self.assertIn('data-report-type="delivery_issue"', html)
        self.assertEqual(html.count('data-bs-target="#failedReceiptModal"'), 2)
        self.assertIn('/check-status?store=ama%20store', html)

    def assert_duplicate(self):
        response = self.client.post('/api/failed-receipts', json=self.payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['code'], 'duplicate_receipt')
        self.assertEqual(response.json['message'],
                         'This issue has Already Being Submitted,Please wait , we are working on it')

    def test_admin_date_range_includes_entire_last_day(self):
        from datetime import datetime, timezone
        self.client.post('/api/failed-receipts', json=self.payload)
        original = self.db.failed_receipts.find_one()
        self.db.failed_receipts.delete_many({})
        from copy import deepcopy
        for index, day in enumerate([datetime(2026, 10, 30, 23, 59), datetime(2026, 10, 31),
                                     datetime(2026, 10, 31, 23, 59, 59), datetime(2026, 11, 1)]):
            record = deepcopy(original)
            record.update(_id=str(index), submitted_at=day.replace(tzinfo=timezone.utc))
            record.pop('duplicate_keys', None)
            record['entries'] = [dict(record['entries'][0], phone=f'024000000{index}')]
            self.db.failed_receipts.insert_one(record)
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
        html = self.client.get('/admin/failed-receipts?date_from=2026-10-31&date_to=2026-10-31').get_data(as_text=True)
        for index in (1, 2):
            self.assertIn(f'data-phone="024000000{index}"', html)
        for index in (0, 3):
            self.assertNotIn(f'data-phone="024000000{index}"', html)
        self.assertIn('All (2)', html)
        for params in ('date_from=bad', 'date_from=2026-11-01&date_to=2026-10-31', 'phone=invalid'):
            self.assertEqual(self.client.get('/admin/failed-receipts?' + params).status_code, 400)

    def test_admin_service_phone_and_status_match_same_entry(self):
        self.client.post('/api/failed-receipts', json=self.payload)
        self.db.failed_receipts.update_one({}, {'$set': {'entries.1.service_name': 'Telecel', 'entries.1.status': 'resolved'}})
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
        empty = self.client.get('/admin/failed-receipts?service=MTN&phone=0200000000').get_data(as_text=True)
        self.assertNotIn('data-phone=', empty)
        html = self.client.get('/admin/failed-receipts?service=Telecel&phone=%2B233200000000&status=resolved').get_data(as_text=True)
        self.assertIn('data-phone="0200000000"', html)
        self.assertNotIn('data-phone="0240000000"', html)
        self.assertIn('Resolved (1)', html)
        self.assertIn('All (1)', html)
        partial = self.client.get('/admin/failed-receipts?phone=024').get_data(as_text=True)
        self.assertIn('data-phone="0240000000"', partial)
        self.assertNotIn('data-phone="0200000000"', partial)

    def test_admin_filters_survive_pagination_and_status_type_links(self):
        from copy import deepcopy
        from datetime import datetime
        from html import unescape
        self.client.post('/api/failed-receipts', json=self.payload)
        original = self.db.failed_receipts.find_one()
        self.db.failed_receipts.delete_many({})
        for index in range(21):
            record = deepcopy(original)
            record.update(_id=str(index), submitted_at=datetime(2026, 10, 31))
            record.pop('duplicate_keys', None)
            self.db.failed_receipts.insert_one(record)
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
        html = unescape(self.client.get('/admin/failed-receipts?date_from=2026-10-31&service=MTN&phone=024&report_type=no_receipt&status=unresolved').get_data(as_text=True))
        from urllib.parse import urlparse, parse_qs
        import re
        next_link = re.search(r'href="([^"]+)">Next</a>', html).group(1)
        params = parse_qs(urlparse(next_link).query)
        self.assertEqual(params, dict(page=['2'], date_from=['2026-10-31'], service=['MTN'], phone=['024'], report_type=['no_receipt'], status=['unresolved']))
        all_types = re.search(r'href="([^"]+)">All report types</a>', html).group(1)
        self.assertNotIn('report_type', parse_qs(urlparse(all_types).query))
        all_statuses = re.search(r'href="([^"]+)">All \(', html).group(1)
        self.assertNotIn('status', parse_qs(urlparse(all_statuses).query))

    def test_same_phone_and_service_blocked_even_with_different_offer(self):
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.payload['submission_id'] = str(uuid4())
        self.payload['entries'] = [{'phone': '+233240000000', 'service_id': 'mtn', 'offer_index': 1}]
        self.assert_duplicate()
        self.assertEqual(self.db.failed_receipts.count_documents({}), 1)

    def test_switching_button_cannot_submit_same_issue_again(self):
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.payload.update(submission_id=str(uuid4()), report_type='delivery_issue')
        self.assert_duplicate()
        self.assertEqual(self.db.failed_receipts.count_documents({}), 1)

    def test_international_phone_in_legacy_report_blocks_duplicate(self):
        for phone in ('233240000000', '+233240000000'):
            with self.subTest(phone=phone):
                self.db.failed_receipts.delete_many({})
                self.db.failed_receipts.insert_one({'entries': [{'phone': phone, 'service_name': ' mtn '}]})
                self.assert_duplicate()

    def test_same_phone_with_different_service_is_allowed(self):
        self.services[0]['name'] = 'MTN NORMAL'
        self.services.append({'_id_str': 'express', 'name': 'MTN EXPRESS',
                              'offers': [{'value_text': '5GB', 'total': 30}]})
        self.payload['catalog_token'] = self.catalog_token()
        self.payload['entries'] = [{'phone': '0599205294', 'service_id': 'mtn', 'offer_index': 0},
                                   {'phone': '0599205294', 'service_id': 'express', 'offer_index': 0}]
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.payload['submission_id'] = str(uuid4())
        self.assert_duplicate()
        self.assertEqual(self.db.failed_receipts.count_documents({}), 1)

    def test_duplicates_in_same_batch_reject_entire_batch(self):
        self.payload['entries'].append({'phone': '+233240000000', 'service_id': 'mtn', 'offer_index': 1})
        self.assert_duplicate()
        self.assertEqual(self.db.failed_receipts.count_documents({}), 0)

    def test_duplicate_check_crosses_pages_and_uses_service_name(self):
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.db.stores.insert_one({'slug': 'ama', 'name': 'Ama Store'})
        self.services[0].update(_id_str='store-mtn', name='  mtn  ')
        self.payload.update(source='store', store_slug='ama', submission_id=str(uuid4()),
                            catalog_token=self.catalog_token('store', 'ama'))
        self.payload['entries'] = [{'phone': '0240000000', 'service_id': 'store-mtn', 'offer_index': 0}]
        self.assert_duplicate()
        self.assertEqual(self.db.failed_receipts.count_documents({}), 1)

    def test_older_unresolved_reports_block_but_resolved_reports_allow_new_issue(self):
        self.db.failed_receipts.insert_one({'entries': [
            {'phone': '0240000000', 'service_name': 'MTN'}]})
        self.assert_duplicate()
        self.assertEqual(self.db.failed_receipts.count_documents({}), 1)
        self.db.failed_receipts.update_one({}, {'$set': {'entries.0.status': 'resolved'}})
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.payload['submission_id'] = str(uuid4())
        self.assert_duplicate()

    def test_resolved_reports_release_old_duplicate_keys_only_for_resolved_entries(self):
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.db.failed_receipts.update_one({}, {'$set': {'entries.0.status': 'resolved'}})
        self.payload.update(submission_id=str(uuid4()), report_type='delivery_issue')
        self.payload['entries'] = [self.payload['entries'][0]]
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        old = self.db.failed_receipts.find_one({'_id': {'$ne': self.payload['submission_id']}})
        self.assertEqual(old['duplicate_keys'], ['0200000000:mtn'])
        self.payload.update(submission_id=str(uuid4()))
        self.assert_duplicate()

    def test_manual_resolution_allows_new_report_but_cannot_reopen_old_one_over_it(self):
        self.client.post('/api/failed-receipts', json=self.payload)
        original_id = self.payload['submission_id']
        with self.client.session_transaction() as session:
            session.update(role='admin', user_id=str(ObjectId()))
        path = f'/admin/failed-receipts/{original_id}/0/status'
        self.client.post(path, data={'csrf_token': 'test-token', 'status': 'resolved'})
        self.assertEqual(self.db.failed_receipts.find_one({'_id': original_id})['duplicate_keys'], ['0200000000:mtn'])
        self.payload.update(submission_id=str(uuid4()))
        self.payload['entries'] = [self.payload['entries'][0]]
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.client.post(path, data={'csrf_token': 'test-token', 'status': 'unresolved'})
        self.assertEqual(self.db.failed_receipts.find_one({'_id': original_id})['entries'][0]['status'], 'resolved')

    def test_phone_and_service_must_match_the_same_previous_entry(self):
        self.db.failed_receipts.insert_one({'entries': [
            {'phone': '0240000000', 'service_name': 'MTN EXPRESS'},
            {'phone': '0551234567', 'service_name': 'MTN'}]})
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)

    def test_all_closed_statuses_allow_new_reports_on_both_pages_and_buttons(self):
        self.db.stores.insert_one({'slug': 'ama', 'name': 'Ama Store'})
        with self.client.session_transaction() as session:
            session.update(role='admin', user_id=str(ObjectId()))
        for status in ('resolved', 'declined', 'rejected', 'cancelled'):
            for source in ('index', 'store'):
                for report_type in ('no_receipt', 'delivery_issue'):
                    with self.subTest(status=status, source=source, report_type=report_type):
                        self.db.failed_receipts.delete_many({})
                        slug = 'ama' if source == 'store' else ''
                        self.payload.update(submission_id=str(uuid4()), source=source, store_slug=slug,
                                            catalog_token=self.catalog_token(source, slug), report_type=report_type)
                        self.payload['entries'] = [{'phone': '0240000000', 'service_id': 'mtn', 'offer_index': 0}]
                        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
                        original_id = self.payload['submission_id']
                        self.payload['submission_id'] = str(uuid4())
                        self.assert_duplicate()
                        status_url = f'/admin/failed-receipts/{original_id}/0/status'
                        self.assertEqual(self.client.post(status_url, data={'csrf_token': 'test-token', 'status': status}).status_code, 302)
                        self.assertNotIn('duplicate_keys', self.db.failed_receipts.find_one({'_id': original_id}))
                        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
                        self.payload['submission_id'] = str(uuid4())
                        self.assert_duplicate()
                        # A closed report cannot be reopened over the new active report.
                        self.client.post(status_url, data={'csrf_token': 'test-token', 'status': 'unresolved'})
                        self.assertEqual(self.db.failed_receipts.find_one({'_id': original_id})['entries'][0]['status'], status)

    def test_old_closed_reports_release_stale_keys_without_unblocking_unresolved_rows(self):
        for status in ('declined', 'rejected', 'cancelled'):
            with self.subTest(status=status):
                self.db.failed_receipts.delete_many({})
                self.payload.update(submission_id=str(uuid4()))
                self.payload['entries'] = [{'phone': '0240000000', 'service_id': 'mtn', 'offer_index': 0},
                                          {'phone': '0200000000', 'service_id': 'mtn', 'offer_index': 1}]
                self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
                original_id = self.payload['submission_id']
                self.db.failed_receipts.update_one({'_id': original_id}, {'$set': {'entries.0.status': status}})
                self.payload.update(submission_id=str(uuid4()))
                self.payload['entries'] = [self.payload['entries'][0]]
                self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
                self.assertEqual(self.db.failed_receipts.find_one({'_id': original_id})['duplicate_keys'], ['0200000000:mtn'])
                self.payload.update(submission_id=str(uuid4()))
                self.payload['entries'][0]['phone'] = '0200000000'
                self.assert_duplicate()

    def test_simultaneous_duplicate_insert_returns_popup_code(self):
        from pymongo.errors import DuplicateKeyError
        with patch.object(self.module.reports_col, 'insert_one', side_effect=DuplicateKeyError('duplicate issue')):
            self.assert_duplicate()

    def test_batch_limit_and_changed_retry_are_rejected(self):
        self.payload['entries'] *= 26
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 400)
        self.payload['entries'] = [{'phone': '0240000000', 'service_id': 'mtn', 'offer_index': 0}]
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        self.payload['entries'][0]['offer_index'] = 1
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 409)
        self.assertEqual(self.db.failed_receipts.find_one()['entries'][0]['offer'], '5GB')

    def test_store_metadata_comes_from_database(self):
        owner = ObjectId()
        self.db.users.insert_one({'_id': owner, 'first_name': 'Ama', 'last_name': 'Mensah'})
        self.db.stores.insert_one({'slug': 'ama', 'name': 'Ama Store', 'owner_id': str(owner)})
        self.payload.update(source='store', store_slug='ama', store_name='Forged name')
        self.payload['catalog_token'] = self.catalog_token('store', 'ama')
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 201)
        report = self.db.failed_receipts.find_one()
        self.assertEqual(report['store_name'], 'Ama Store')
        self.assertEqual(report['store_owner_name'], 'Ama Mensah')
        self.assertEqual(report['customer_name'], 'Guest buyer')
        self.payload.update(store_slug='missing', submission_id=str(uuid4()))
        self.payload['catalog_token'] = self.catalog_token('store', 'missing')
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 404)

    def test_admin_page_access_totals_and_copy_buttons(self):
        self.client.post('/api/failed-receipts', json=self.payload)
        self.assertEqual(self.client.get('/admin/failed-receipts').status_code, 302)
        with self.client.session_transaction() as session:
            session['role'] = 'admin'
        response = self.client.get('/admin/failed-receipts')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Total sent (purchases)', html)
        self.assertIn('data-phone="0240000000"', html)
        self.assertIn('Index page', html)
        self.assertIn('Guest buyer', html)
        self.assertIn('<strong>MTN</strong>', html)

    def test_only_page_services_and_service_offers_are_accepted(self):
        self.payload['entries'][0]['service_id'] = 'another-store-service'
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 400)
        self.payload['entries'][0]['service_id'] = 'mtn'
        self.payload['entries'][0]['offer_index'] = 99
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 400)
        self.payload['entries'][0]['offer_index'] = 0
        self.payload.update(source='store', store_slug='ama')
        self.assertEqual(self.client.post('/api/failed-receipts', json=self.payload).status_code, 400)
        self.assertEqual(self.db.failed_receipts.count_documents({}), 0)

    def test_catalog_uses_each_pages_normalized_offers(self):
        with self.app.app_context():
            choices = self.module.receipt_catalog(self.services, 'index')['services']
            self.assertEqual(choices[0]['image_url'], 'https://example.com/mtn.png')
            self.assertEqual(choices[0]['offers'][0], {'label': '5GB', 'amount': 30, 'value': None})
            store_services = [{'_id_str': 'telecel', 'name': 'Telecel', 'offers': [{'value_text': '10GB', 'total': 50}]}]
            choices = self.module.receipt_catalog(store_services, 'store', 'ama')['services']
            self.assertEqual([service['id'] for service in choices], ['telecel'])
            self.assertEqual(choices[0]['offers'][0]['label'], '10GB')

    def test_shared_form_renders_for_both_sources(self):
        for source in ['index', 'store']:
            with self.app.test_request_context('/'):
                html = render_template('failed_receipt_form.html', receipt_source=source, store={'slug': 'ama'}, services=self.services)
                self.assertIn('Add another', html)
                self.assertIn(f'source:"{source}"', html)
                self.assertIn('Submit', html)
                self.assertIn('receipt-service-trigger', html)
                self.assertIn('<select class="form-select receipt-offer"', html)
                self.assertNotIn('type="text"', html)
                self.assertIn('Check Status', html)
                self.assertIn('/failed-receipts/status', html)
                self.assertIn("if (result.code === 'duplicate_receipt') window.alert(result.message);", html)

    def test_public_status_matches_number_and_hides_other_bulk_entries(self):
        self.client.post('/api/failed-receipts', json=self.payload)
        self.db.failed_receipts.update_one({}, {'$set': {
            'entries.0.status': 'declined', 'entries.1.offer': 'PRIVATE-OFFER',
            'customer_name': 'PRIVATE-CUSTOMER'}})
        response = self.client.get('/failed-receipts/status', query_string={'phone': '+233240000000'})
        self.assertEqual(200, response.status_code)
        html = response.get_data(as_text=True)
        self.assertIn('Declined', html)
        self.assertIn('5GB', html)
        self.assertIn('1 report(s) found', html)
        self.assertNotIn('PRIVATE-OFFER', html)
        self.assertNotIn('PRIVATE-CUSTOMER', html)
        self.assertEqual('no-store', response.headers['Cache-Control'])
        self.assertEqual(400, self.client.get('/failed-receipts/status?phone=invalid').status_code)
        self.assertIn('No Failed/No Receipt reports', self.client.get(
            '/failed-receipts/status?phone=0551234567').get_data(as_text=True))

    def test_declined_status_can_be_saved_and_filtered_separately(self):
        self.client.post('/api/failed-receipts', json=self.payload)
        with self.client.session_transaction() as session:
            session.update(role='admin', user_id=str(ObjectId()))
        report = self.db.failed_receipts.find_one()
        response = self.client.post(f'/admin/failed-receipts/{report["_id"]}/0/status',
            data={'csrf_token': 'test-token', 'status': 'declined'})
        self.assertEqual(302, response.status_code)
        self.assertEqual('declined', self.db.failed_receipts.find_one()['entries'][0]['status'])
        html = self.client.get('/admin/failed-receipts?status=declined').get_data(as_text=True)
        self.assertIn('Declined (1)', html)
        self.assertIn('Unresolved (1)', html)
        self.assertNotIn('0200000000', html)
        for status in ('rejected', 'cancelled'):
            with self.subTest(status=status):
                response = self.client.post(f'/admin/failed-receipts/{report["_id"]}/0/status',
                    data={'csrf_token': 'test-token', 'status': status})
                self.assertEqual(302, response.status_code)
                self.assertEqual(status, self.db.failed_receipts.find_one()['entries'][0]['status'])
                html = self.client.get('/admin/failed-receipts', query_string={'status': status}).get_data(as_text=True)
                self.assertIn(f'{status.capitalize()} (1)', html)
                self.assertIn('Unresolved (1)', html)
                self.assertIn(status.capitalize(), self.client.get(
                    '/failed-receipts/status?phone=0240000000').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
