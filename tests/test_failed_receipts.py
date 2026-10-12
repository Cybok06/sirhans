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


if __name__ == '__main__':
    unittest.main()
