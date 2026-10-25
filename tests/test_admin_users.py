"""Managed admin integration tests using an in-memory database only."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime
from html.parser import HTMLParser
from unittest.mock import patch

import mongomock
from flask import Flask, render_template
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProviderBadges(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.labels = []
        self.active = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.active = tag == 'span' and (
            'prov-badge' in attrs.get('class', '').split()
            or attrs.get('data-role') == 'provider-badge')

    def handle_data(self, data):
        if self.active:
            self.labels.append(data.strip())

    def handle_endtag(self, tag):
        self.active = False


class AdminUsersTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test
        with patch.dict(sys.modules, {"db": types.SimpleNamespace(db=self.db)}):
            self.access = load_module("test_access", "admin_access.py")
            with patch.dict(sys.modules, {"admin_access": self.access}):
                self.users = load_module("test_users", "admin_users.py")
                self.login = load_module("test_login", "login.py")
        self.login.ENABLE_IP_LOOKUP = False
        self.app = Flask(__name__, template_folder=str(ROOT / "templates"))
        self.app.config.update(TESTING=True, SECRET_KEY="test-only")
        self.access.init_admin_access(self.app)
        self.app.register_blueprint(self.users.admin_users_bp)
        self.app.register_blueprint(self.login.login_bp)
        for key, _, endpoint, _ in self.access.ADMIN_PAGES:
            self.app.add_url_rule('/test/' + key, endpoint, lambda: 'allowed', methods=['GET', 'POST'])
        self.app.add_url_rule('/admin/profile', 'admin_profile.admin_profile', lambda: 'full admin', methods=['GET', 'POST'])
        self.app.add_url_rule('/customer', 'customer_dashboard.customer_dashboard', lambda: 'customer')
        self.app.add_url_rule('/new-admin-action', 'unknown.action', lambda: 'unexpected', methods=['POST'])
        self.app.add_url_rule('/sidebar', 'admin_orders.sidebar', lambda: render_template('admin_sidebar.html'))
        self.app.context_processor(lambda: dict(pending_customers_count=0, undelivered_orders_count=0,
                                               blocked_phone_numbers_count=0, pending_complaints_count=0))
        self.client = self.app.test_client()
        self.owner = self.db.users.insert_one(dict(username='owner', role='admin', status='active')).inserted_id
        self.staff = self.db.users.insert_one(dict(username='staff', name='Staff', email='staff@example.com',
            role='admin', status='active', managed_admin=True, allowed_pages=['orders', 'services'],
            password=generate_password_hash('password123'), access_version=0)).inserted_id
        self.sign_in(self.owner)

    def sign_in(self, uid, role='admin'):
        with self.client.session_transaction() as session:
            session.clear()
            session.update(user_id=str(uid), role=role, admin_logged_in=role == 'admin', users_csrf='test-token', access_version=0)

    def post(self, **data):
        return self.client.post('/admin/users', data={'csrf_token': 'test-token', **data})

    def creation(self, **overrides):
        return dict(action='create', name='New User', username='new_user', email='new@example.com',
                    password='password123', confirm_password='password123', allowed_pages=['orders', 'services'], **overrides)

    def test_create_hashes_password_and_stores_permissions(self):
        self.assertEqual(self.post(**self.creation()).status_code, 302)
        user = self.db.users.find_one({'username': 'new_user'})
        self.assertEqual(user['allowed_pages'], ['orders', 'services'])
        self.assertTrue(user['managed_admin'])
        self.assertTrue(check_password_hash(user['password'], 'password123'))
        self.assertNotEqual(user['password'], 'password123')

    def test_invalid_fields_preserve_form_without_creating_user(self):
        for changes in [dict(allowed_pages=[]), dict(allowed_pages=['users']), dict(username='x'),
                        dict(email='invalid'), dict(password='short'), dict(confirm_password='different'),
                        dict(username='STAFF'), dict(email='STAFF@example.com')]:
            data = self.creation()
            data.update(changes)
            with self.subTest(changes=changes):
                response = self.post(**data)
                self.assertEqual(response.status_code, 400)
                self.assertIn(b'New User', response.data)
                self.assertEqual(self.db.users.count_documents({}), 2)

    def test_requires_csrf_and_full_admin(self):
        self.assertEqual(self.client.post('/admin/users', data=self.creation()).status_code, 400)
        self.sign_in(self.staff)
        self.assertEqual(self.post(**self.creation()).status_code, 403)
        self.assertEqual(self.client.get('/admin/users').status_code, 403)
        self.assertEqual(self.client.post('/admin/profile', data={'action': 'create_admin'}).status_code, 403)
        self.sign_in(self.staff, 'customer')
        self.assertEqual(self.post(**self.creation()).status_code, 302)

    def test_direct_pages_actions_and_unknown_endpoints_are_denied(self):
        self.sign_in(self.staff)
        for page in ['services', 'orders']:
            self.assertEqual(self.client.get('/test/' + page).status_code, 200)
            self.assertEqual(self.client.post('/test/' + page).status_code, 200)
        for path in ['/test/customers', '/test/dashboard', '/admin/profile', '/new-admin-action', '/customer']:
            self.assertEqual(self.client.post(path).status_code if path != '/customer' else self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get('/test/customers', headers={'Accept': 'application/json'}).json['status'], 'error')

    def test_mobile_and_desktop_sidebar_only_show_assigned_pages(self):
        self.sign_in(self.staff)
        html = self.client.get('/sidebar').get_data(as_text=True)
        self.assertEqual(html.count('nav-label">Orders</span>'), 2)
        self.assertEqual(html.count('nav-label">Services</span>'), 2)
        for label in ['Users', 'Dashboard', 'Customers', 'Admin Profile']:
            self.assertNotIn('nav-label">' + label + '</span>', html)
        self.sign_in(self.owner)
        self.assertEqual(self.client.get('/sidebar').get_data(as_text=True).count('nav-label">Users</span>'), 2)

    def test_login_lands_on_assigned_page(self):
        self.client.get('/logout')
        response = self.client.post('/login', data={'username': 'staff', 'password': 'password123'})
        self.assertTrue(response.location.endswith('/test/services'))

    def test_update_permissions_applies_to_existing_session(self):
        response = self.post(action='update', user_id=str(self.staff), name='Staff', username='staff',
                             email='staff@example.com', allowed_pages=['orders'], password='', confirm_password='')
        self.assertEqual(response.status_code, 302)
        self.sign_in(self.staff)
        self.assertEqual(self.client.get('/test/services').status_code, 403)
        self.assertEqual(self.client.get('/test/orders').status_code, 200)
        self.db.users.update_one({'_id': self.staff}, {'$set': {'allowed_pages': []}})
        self.assertEqual(self.client.get('/test/orders').status_code, 403)
        self.assertEqual(self.client.get('/admin/no-access').status_code, 200)

    def test_disable_and_enable_invalidates_old_sessions(self):
        for status in ['blocked', 'active']:
            self.assertEqual(self.post(action='status', user_id=str(self.staff), status=status).status_code, 302)
        self.sign_in(self.staff)
        self.assertEqual(self.client.get('/test/orders').status_code, 302)
        with self.client.session_transaction() as session:
            self.assertNotIn('user_id', session)

    def test_password_reset_invalidates_sessions(self):
        self.assertEqual(self.post(action='update', user_id=str(self.staff), name='Staff', username='staff',
            email='staff@example.com', allowed_pages=['orders'], password='new-password', confirm_password='new-password').status_code, 302)
        self.sign_in(self.staff)
        self.assertEqual(self.client.get('/test/orders').status_code, 302)
        self.assertTrue(check_password_hash(self.db.users.find_one({'_id': self.staff})['password'], 'new-password'))

    def test_cannot_modify_full_admin_or_customer(self):
        customer = self.db.users.insert_one({'role': 'customer', 'username': 'customer'}).inserted_id
        for uid in [self.owner, customer]:
            self.assertEqual(self.post(action='status', user_id=str(uid), status='blocked').status_code, 404)

    def test_blocked_login_is_refused(self):
        self.db.users.update_one({'_id': self.staff}, {'$set': {'status': 'blocked'}})
        self.client.get('/logout')
        self.assertEqual(self.client.post('/login', data={'username': 'staff', 'password': 'password123'}).status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn('user_id', session)

    def attach_services(self):
        with patch.dict(sys.modules, {"db": types.SimpleNamespace(db=self.db), "admin_access": self.access}):
            services = load_module("test_services", "admin_services.py")
        del self.app.view_functions['admin_services.manage_services']
        self.app.register_blueprint(services.admin_services_bp)
        return self.db.services.insert_one({"name": "MTN", "provider": "dataconnect", "offers": [],
                                            "image_url": "/image.png"}).inserted_id

    def test_provider_changes_require_full_admin_for_json_and_form(self):
        service_id = self.attach_services()
        self.sign_in(self.staff)
        for payload in [{'json': {'provider': 'bundleportal'}}, {'data': {'provider': 'portal02'}}]:
            response = self.client.post(f'/admin/services/{service_id}/provider', **payload)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(self.db.services.find_one({'_id': service_id})['provider'], 'dataconnect')
        self.sign_in(self.owner)
        self.assertEqual(self.client.post(f'/admin/services/{service_id}/provider', json={'provider': 'bundleportal'}).status_code, 200)
        self.assertEqual(self.db.services.find_one({'_id': service_id})['provider'], 'bundleportal')

    def test_provider_controls_hidden_only_for_managed_users(self):
        service_id = self.attach_services()
        for uid, visible in [(self.staff, False), (self.owner, True)]:
            self.sign_in(uid)
            response = self.client.get('/admin/services')
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertEqual(f'<select id="provider-{service_id}"' in html, visible)
            self.assertEqual('<select name="provider"' in html, visible)

    def test_service_creation_and_price_edit_cannot_override_provider(self):
        service_id = self.attach_services()
        self.sign_in(self.staff)
        fields = {'service_name': 'New service', 'image_url': '/image.png'}
        self.assertEqual(self.client.post('/admin/services/create', data={**fields, 'provider': 'skplug'}).status_code, 403)
        self.assertEqual(self.db.services.count_documents({}), 1)
        self.assertEqual(self.client.post('/admin/services/create', data=fields).status_code, 302)
        self.assertEqual(self.db.services.find_one({'name': 'New service'})['provider'], 'portal02')
        self.assertEqual(self.client.post(f'/admin/services/{service_id}/update', data={'provider': 'skplug'}).status_code, 302)
        self.assertEqual(self.db.services.find_one({'_id': service_id})['provider'], 'dataconnect')
        self.sign_in(self.owner)
        self.assertEqual(self.client.post('/admin/services/create', data={**fields, 'service_name': 'Owner service', 'provider': 'skplug'}).status_code, 302)
        self.assertEqual(self.db.services.find_one({'name': 'Owner service'})['provider'], 'skplug')

    PROVIDERS = {
        'bundleportal': 'BNP', 'portal02': 'PT2', 'dataconnect': 'DTC',
        'datakazina': 'DTK', 'codecraft': 'CDK', 'skplug': 'SKP',
        'bundleportal_mtn': 'BNP', 'bundleportal_mtn2': 'BNP',
        'bundleportal_mtn3': 'BNP', 'bundleportal_ishare': 'BNP',
        'bundleportal_telecel': 'BNP',
    }

    def assert_no_provider_names(self, html):
        for name in ['bundleportal', 'bundle portal', 'portal02', 'portal-02',
                     'dataconnect', 'datakazina', 'codecraft', 'skplug']:
            self.assertNotIn(name, html.lower())

    def test_managed_services_use_codes_in_badges_and_html_attributes(self):
        self.attach_services()
        self.db.services.delete_many({})
        for provider in self.PROVIDERS:
            self.db.services.insert_one(dict(name='MTN Normal', provider=provider, offers=[]))
        self.sign_in(self.staff)
        response = self.client.get('/admin/services')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertCountEqual(ProviderBadges(html).labels, self.PROVIDERS.values())
        self.assert_no_provider_names(html)
        for code in set(self.PROVIDERS.values()):
            self.assertIn(f'data-provider="{code}"', html)
        self.assertNotIn('<select name="provider"', html)
        self.assertEqual({row['provider'] for row in self.db.services.find()}, set(self.PROVIDERS))
        self.sign_in(self.owner)
        html = self.client.get('/admin/services').get_data(as_text=True)
        self.assertCountEqual(ProviderBadges(html).labels, self.PROVIDERS)
        for provider in self.PROVIDERS:
            self.assertIn(f'data-provider="{provider}"', html)
        self.assertIn('<select name="provider"', html)

    def attach_orders(self):
        # Keep extension modules loaded outside patch.dict's module cleanup.
        importlib.import_module('pandas')
        importlib.import_module('reportlab.platypus')
        with patch.dict(sys.modules, {"db": types.SimpleNamespace(db=self.db), "admin_access": self.access}):
            orders = load_module("test_orders", "admin_orders.py")
        del self.app.view_functions['admin_orders.admin_view_orders']
        self.app.register_blueprint(orders.admin_orders_bp)
        return orders

    def test_managed_orders_mask_desktop_mobile_and_summary_provider_badges(self):
        self.attach_orders()
        items = [dict(serviceName='MTN', phone='0200000000', value='1GB',
                      amount=10, provider=provider, api_status='processing') for provider in self.PROVIDERS]
        oid = self.db.orders.insert_one(dict(order_id='ORDER-1', user_id=self.staff,
            items=items, created_at=datetime.utcnow(), status='processing', charged_amount=110)).inserted_id
        self.sign_in(self.staff)
        response = self.client.get('/admin/orders')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        # Each supplier appears in desktop items, the provider summary, and mobile items.
        self.assertCountEqual(ProviderBadges(html).labels, list(self.PROVIDERS.values()) * 3)
        self.assert_no_provider_names(html)
        self.assertEqual(self.db.orders.find_one({'_id': oid})['items'], items)
        self.sign_in(self.owner)
        html = self.client.get('/admin/orders').get_data(as_text=True)
        names = {'portal02': 'Portal-02', 'dataconnect': 'DataConnect',
                 'datakazina': 'DataKazina', 'codecraft': 'CodeCraft', 'skplug': 'SKPlug'}
        self.assertCountEqual(ProviderBadges(html).labels,
                             [names.get(provider, provider) for provider in self.PROVIDERS] * 3)

    def test_provider_codes_handle_legacy_names_unknown_and_empty_values(self):
        cases = {'Bundle Portal': 'BNP', 'Bundle Portal MTN2': 'BNP',
                 'Portal 02': 'PT2', 'Portal-02': 'PT2', 'portal002': 'PT2',
                 'Data connect': 'DTC', 'DConnect': 'DTC', 'DataKazina': 'DTK',
                 'DakaZina': 'DTK', 'CodeCraft': 'CDK', 'SK Plug': 'SKP',
                 'Private Supplier': 'Provider', '': '', None: '', 123: 'Provider'}
        with self.app.test_request_context():
            from flask import g
            # Legacy accounts with allowed_pages are also restricted admins.
            g.admin_user = {'role': 'admin', 'allowed_pages': ['services']}
            for value, code in cases.items():
                with self.subTest(value=value):
                    self.assertEqual(self.access.provider_display_name(value), code)
            g.admin_user = {'role': 'admin'}
            for value in cases:
                self.assertEqual(self.access.provider_display_name(value), str(value or ''))

    def test_order_status_and_schedule_errors_mask_provider_names(self):
        self.attach_orders()
        message = 'Bundle Portal MTN2 / Portal 02 / Data Connect / DataKazina / CodeCraft / SKplug unavailable; bundleportal_submitting'
        masked = 'BNP MTN2 / PT2 / DTC / DTK / CDK / SKP unavailable; BNP_submitting'
        self.db.orders.insert_one(dict(order_id='ORDER-2', user_id=self.staff,
            created_at=datetime.utcnow(), status='processing', charged_amount=10,
            items=[dict(serviceName='MTN', phone='0200000000', value='1GB',
                        amount=10, provider='bundleportal_mtn2', api_status=message)]))
        self.db.order_schedules.insert_one(dict(state='error', result={'errors': [message]}))
        self.sign_in(self.staff)
        html = self.client.get('/admin/orders').get_data(as_text=True)
        self.assertEqual(html.count(masked), 2)
        self.assert_no_provider_names(html)
        result = self.client.get('/admin/orders/schedules').json['jobs'][0]['result']
        self.assertEqual(result['errors'], [masked])
        self.assertEqual(self.db.order_schedules.find_one()['result']['errors'], [message])
        self.sign_in(self.owner)
        html = self.client.get('/admin/orders').get_data(as_text=True)
        self.assertEqual(html.count(message), 2)
        result = self.client.get('/admin/orders/schedules').json['jobs'][0]['result']
        self.assertEqual(result['errors'], [message])


if __name__ == '__main__':
    unittest.main()
