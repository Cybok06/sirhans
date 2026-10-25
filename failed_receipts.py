"""Public batch receipt reports and their admin inbox."""
import re
import secrets
from datetime import datetime, timezone
from uuid import UUID

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pymongo.errors import DuplicateKeyError, PyMongoError

from db import db

failed_receipts_bp = Blueprint('failed_receipts', __name__)
reports_col = db['failed_receipts']
<<<<<<< HEAD
DUPLICATE_MESSAGE = 'This issue has Already Being Submitted,Please wait , we are working on it'


def receipt_issue_key(entry):
    """An issue belongs to a phone and service name, regardless of offer or page."""
    service = ' '.join(str(entry.get('service_name') or '').split()).casefold()
    return entry['phone'] + ':' + service


def duplicate_receipt_response():
    return jsonify(success=False, code='duplicate_receipt', message=DUPLICATE_MESSAGE), 409


def receipt_retry_response(existing, report):
    identity = lambda rows: [tuple(row.get(key) for key in ('phone', 'service_id', 'offer_index', 'offer')) for row in rows]
    if (identity(existing.get('entries', [])) != identity(report['entries'])
            or any(existing.get(key) != report.get(key) for key in ('source', 'store_slug'))):
        return jsonify(success=False, message='Please refresh before submitting a new report.'), 409
    return jsonify(success=True, message=f'{len(report["entries"])} receipt report(s) sent to admin.'), 201
=======
>>>>>>> origin/main


def receipt_catalog(services, source, slug=''):
    """Use the same normalized services/offers that the current page displays."""
    choices = []
    for service in services:
        offers = [{'label': str(offer.get('value_text') or offer.get('value') or 'Offer'),
                   'amount': offer.get('total', offer.get('amount')), 'value': offer.get('value')} for offer in service.get('offers', [])]
        if offers:
            choices.append({'id': str(service.get('_id_str') or service.get('_id') or service.get('name')),
                            'name': str(service.get('name') or 'Service'),
                            'image_url': str(service.get('image_url') or ''), 'offers': offers})
    catalog = {'source': source, 'store_slug': slug, 'services': choices}
    token = URLSafeTimedSerializer(current_app.secret_key, salt='receipt-catalog').dumps(catalog)
    return {'services': choices, 'token': token}


@failed_receipts_bp.app_context_processor
def receipt_form_context():
    if 'receipt_csrf' not in session:
        session['receipt_csrf'] = secrets.token_urlsafe(32)
    return {'receipt_csrf': session['receipt_csrf'], 'receipt_catalog': receipt_catalog}


@failed_receipts_bp.app_context_processor
def receipt_sidebar_context():
    total = 0
    if session.get('role') == 'admin':
        try:
            counts = list(reports_col.aggregate([{'$group': {
                '_id': None,
                'total': {'$sum': {'$size': {'$ifNull': ['$entries', []]}}},
            }}]))
            total = counts[0]['total'] if counts else 0
        except PyMongoError:
            current_app.logger.exception('Unable to count failed receipt submissions')
    return {'failed_receipts_count': total}


def user_name(user):
    return ' '.join(str(user.get(k) or '') for k in ('first_name', 'last_name')).strip() or str(user.get('full_name') or user.get('name') or user.get('username') or 'Unknown')


@failed_receipts_bp.post('/api/failed-receipts')
def submit():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(success=False, message='Please submit a valid form.'), 400
    token = data.get('csrf_token')
    if not isinstance(token, str) or not secrets.compare_digest(token, session.get('receipt_csrf', '')) or not token:
        return jsonify(success=False, message='Please refresh the page and try again.'), 403
    source = data.get('source')
    entries = data.get('entries')
    if source not in ('index', 'store') or not isinstance(entries, list) or not 1 <= len(entries) <= 50:
        return jsonify(success=False, message='Submit between 1 and 50 entries.'), 400
    try:
        catalog = URLSafeTimedSerializer(current_app.secret_key, salt='receipt-catalog').loads(data.get('catalog_token', ''), max_age=86400)
    except (BadSignature, TypeError):
        return jsonify(success=False, message='Please refresh the page to reload the available services and offers.'), 400
    if catalog['source'] != source or catalog['store_slug'] != (data.get('store_slug') or ''):
        return jsonify(success=False, message='The selected services do not belong to this page.'), 400
    services = {service['id']: service for service in catalog['services']}
    clean = []
    for entry in entries:
        if not isinstance(entry, dict):
            return jsonify(success=False, message='Invalid entry.'), 400
        phone = entry.get('phone')
        service_id = entry.get('service_id')
        offer_index = entry.get('offer_index')
        service = services.get(service_id) if isinstance(service_id, str) else None
        if not isinstance(phone, str) or not service or type(offer_index) is not int or not 0 <= offer_index < len(service['offers']):
            return jsonify(success=False, message='Enter a phone number and select a valid service and offer for every entry.'), 400
        offer = service['offers'][offer_index]['label']
        phone = re.sub(r'[\s+()-]', '', phone)
        if re.fullmatch(r'233[0-9]{9}', phone):
            phone = '0' + phone[3:]
        if not re.fullmatch(r'0[0-9]{9}', phone):
            return jsonify(success=False, message='Use a valid Ghana phone number.'), 400
        clean.append({'phone': phone, 'service_id': service_id, 'service_name': service['name'],
                      'offer': offer, 'offer_index': offer_index, 'status': 'unresolved',
                      'offer_value': service['offers'][offer_index].get('value')})
    try:
        submission_id = str(UUID(str(data.get('submission_id'))))
    except (ValueError, TypeError, AttributeError):
        return jsonify(success=False, message='Invalid submission ID. Refresh and try again.'), 400
    store = None
    owner = {}
    if source == 'store':
        slug = data.get('store_slug')
        if not isinstance(slug, str) or len(slug) > 200:
            return jsonify(success=False, message='Invalid store.'), 400
        store = db['stores'].find_one({'slug': slug, 'status': {'$ne': 'deleted'}})
        if not store:
            return jsonify(success=False, message='Store not found.'), 404
        owner_id = store.get('owner_id')
        if ObjectId.is_valid(str(owner_id)):
            owner = db['users'].find_one({'_id': ObjectId(str(owner_id))}) or {}
    customer = {}
    if ObjectId.is_valid(str(session.get('user_id'))):
        customer = db['users'].find_one({'_id': ObjectId(str(session['user_id']))}) or {}
    report = {'_id': submission_id, 'entries': clean, 'entry_count': len(clean),
              'source': source, 'submitted_at': datetime.now(timezone.utc),
              'customer_name': user_name(customer) if customer else 'Guest buyer',
              'customer_id': customer.get('_id'),
              'store_name': (store or {}).get('name', ''), 'store_slug': (store or {}).get('slug', ''),
              'store_owner_name': user_name(owner) if store else '',
              'store_owner_id': (store or {}).get('owner_id')}
    try:
<<<<<<< HEAD
        # Preserve safe retries after a lost response, without creating a report twice.
        existing = reports_col.find_one({'_id': submission_id})
        if existing:
            return receipt_retry_response(existing, report)
        issue_keys = [receipt_issue_key(entry) for entry in clean]
        if len(set(issue_keys)) != len(issue_keys):
            return duplicate_receipt_response()
        # Older reports have no duplicate_keys field; check their entries as well.
        # Match within each entry so a phone on one row cannot match another service.
        phones = [entry['phone'] for entry in clean]
        keys = set(issue_keys)
        for previous in reports_col.find({'entries.phone': {'$in': phones}}, {'entries': 1}):
            if any(receipt_issue_key(entry) in keys for entry in previous.get('entries', []) if entry.get('phone')):
                return duplicate_receipt_response()
        # A unique multikey index also blocks simultaneous submissions of an issue.
        # Index only new reports; pre-existing report documents stay untouched.
        reports_col.create_index('duplicate_keys', unique=True,
                                 partialFilterExpression={'duplicate_keys': {'$exists': True}})
        report['duplicate_keys'] = issue_keys
        reports_col.insert_one(report)
    except DuplicateKeyError:
        existing = reports_col.find_one({'_id': submission_id}) or {}
        if existing:
            return receipt_retry_response(existing, report)
        return duplicate_receipt_response()
=======
        reports_col.insert_one(report)
    except DuplicateKeyError:
        existing = reports_col.find_one({'_id': submission_id}) or {}
        identity = lambda rows: [tuple(row.get(key) for key in ('phone', 'service_id', 'offer_index', 'offer')) for row in rows]
        if (identity(existing.get('entries', [])) != identity(clean)
                or any(existing.get(key) != report.get(key) for key in ('source', 'store_slug'))):
            return jsonify(success=False, message='Please refresh before submitting a new report.'), 409
>>>>>>> origin/main
    except PyMongoError:
        return jsonify(success=False, message='Unable to save your report. Please try again.'), 503
    return jsonify(success=True, message=f'{len(clean)} receipt report(s) sent to admin.'), 201


@failed_receipts_bp.get('/admin/failed-receipts')
def admin_page():
    if session.get('role') != 'admin':
        return redirect(url_for('login.login'))
    page = max(1, request.args.get('page', 1, type=int) or 1)
    status_filter = request.args.get('status', '')
    if status_filter not in ('resolved', 'unresolved', 'declined', 'rejected', 'cancelled'):
        status_filter = ''
    query = {'entries': {'$elemMatch': {'status': {'$nin': ['resolved', 'declined', 'rejected', 'cancelled']} if status_filter == 'unresolved' else status_filter}}} if status_filter else {}
    total_batches = reports_col.count_documents({})
    filtered_batches = reports_col.count_documents(query)
    pages = max(1, (filtered_batches + 19) // 20)
    page = min(page, pages)
    totals = list(reports_col.aggregate([{'$unwind': '$entries'}, {'$group': {
        '_id': None, 'total': {'$sum': 1}, 'resolved': {'$sum': {'$cond': [{'$eq': ['$entries.status', 'resolved']}, 1, 0]}},
        'declined': {'$sum': {'$cond': [{'$eq': ['$entries.status', 'declined']}, 1, 0]}},
        'rejected': {'$sum': {'$cond': [{'$eq': ['$entries.status', 'rejected']}, 1, 0]}},
        'cancelled': {'$sum': {'$cond': [{'$eq': ['$entries.status', 'cancelled']}, 1, 0]}}}}]))
    counts = totals[0] if totals else {'total': 0, 'resolved': 0, 'declined': 0, 'rejected': 0, 'cancelled': 0}
    reports = list(reports_col.find(query).sort('submitted_at', -1).skip((page - 1) * 20).limit(20))
    for report in reports:
        for index, entry in enumerate(report['entries']):
            entry['entry_index'] = index
            entry['status'] = entry.get('status', 'unresolved')
            entry['visible'] = not status_filter or entry['status'] == status_filter
    return render_template('admin_failed_receipts.html', reports=reports, total=counts['total'], resolved=counts['resolved'],
                           unresolved=counts['total'] - sum(counts[key] for key in ('resolved', 'declined', 'rejected', 'cancelled')),
                           declined=counts['declined'], rejected=counts['rejected'], cancelled=counts['cancelled'], status_filter=status_filter,
                           total_batches=total_batches, page=page, pages=pages)


@failed_receipts_bp.get('/failed-receipts/status')
def public_status():
    raw_phone = request.args.get('phone', '').strip()
    phone = re.sub(r'[\s+()-]', '', raw_phone)
    if re.fullmatch(r'233[0-9]{9}', phone):
        phone = '0' + phone[3:]
    error = None
    matches = []
    if raw_phone:
        if not re.fullmatch(r'0[0-9]{9}', phone):
            error = 'Enter a valid Ghana phone number.'
        else:
            # Project only matching entries; never expose other recipients in a
            # bulk submission, buyer identity, or internal placement details.
            for report in reports_col.find({'entries.phone': phone}, {
                'entries': 1, 'submitted_at': 1, 'source': 1, 'store_name': 1,
            }).sort('submitted_at', -1):
                for entry in report.get('entries', []):
                    if entry.get('phone') == phone:
                        matches.append({'submitted_at': report.get('submitted_at'),
                            'store_name': report.get('store_name') if report.get('source') == 'store' else '',
                            'service_name': entry.get('service_name', ''), 'offer': entry.get('offer', ''),
                            'status': entry.get('status', 'unresolved')})
    response = current_app.make_response(render_template('failed_receipt_status.html',
        phone=raw_phone, searched=bool(raw_phone), reports=matches, error=error))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response, 400 if error else 200


import receipt_recovery as receipt_recovery_module
receipt_recovery_module.register_receipt_recovery(failed_receipts_bp, reports_col)
