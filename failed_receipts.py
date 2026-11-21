"""Public batch receipt reports and their admin inbox."""
import re
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pymongo.errors import DuplicateKeyError, PyMongoError

from db import db

failed_receipts_bp = Blueprint('failed_receipts', __name__)
reports_col = db['failed_receipts']
DUPLICATE_MESSAGE = 'This issue has Already Being Submitted,Please wait , we are working on it'
CLOSED_RECEIPT_STATUSES = frozenset(('resolved', 'declined', 'rejected', 'cancelled'))


def receipt_issue_key(entry):
    """An issue belongs to a phone and service name, regardless of offer or page."""
    service = ' '.join(str(entry.get('service_name') or '').split()).casefold()
    return normalized_receipt_phone(entry.get('phone')) + ':' + service


def normalized_receipt_phone(phone):
    phone = re.sub(r'[\s+()-]', '', str(phone or ''))
    return '0' + phone[3:] if re.fullmatch(r'233[0-9]{9}', phone) else phone


def receipt_phone_variants(phone):
    phone = normalized_receipt_phone(phone)
    return [phone, '233' + phone[1:], '+233' + phone[1:]]


def update_receipt_entry(report_id, entry_index, fields, conditions=None):
    """Change status and its active duplicate reservations in one atomic write."""
    for _ in range(5):
        report = reports_col.find_one({'_id': report_id})
        entries = report['entries']
        status = fields.get(f'entries.{entry_index}.status', entries[entry_index].get('status'))
        if status not in CLOSED_RECEIPT_STATUSES and entries[entry_index].get('status') in CLOSED_RECEIPT_STATUSES:
            issue = receipt_issue_key(entries[entry_index])
            phones = receipt_phone_variants(entries[entry_index]['phone'])
            for other in reports_col.find({'_id': {'$ne': report_id}, 'entries.phone': {'$in': phones}}):
                if any(row.get('status') not in CLOSED_RECEIPT_STATUSES and receipt_issue_key(row) == issue for row in other.get('entries', [])):
                    raise DuplicateKeyError('Another unresolved report exists for this issue.')
        keys = list(dict.fromkeys(receipt_issue_key(entry) for index, entry in enumerate(entries)
                                  if (status if index == entry_index else entry.get('status')) not in CLOSED_RECEIPT_STATUSES))
        query = dict(conditions or {}, _id=report_id, entries=entries)
        update = {'$set': dict(fields)}
        if keys:
            update['$set']['duplicate_keys'] = keys
        else:
            update['$unset'] = {'duplicate_keys': ''}
        result = reports_col.update_one(query, update)
        if result.matched_count or conditions:
            return result
    raise PyMongoError('Report changed while updating its duplicate protection.')


def is_new_issue_after_closure(current, previous_report, previous_entry):
    if previous_entry.get('status') not in CLOSED_RECEIPT_STATUSES:
        return False
    resolved_at = previous_entry.get('status_updated_at') or previous_report.get('submitted_at')
    submitted_at = current.get('submitted_at')
    if not resolved_at or not submitted_at:
        return False
    submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    resolved_at = resolved_at.replace(tzinfo=timezone.utc)
    # Mongo dates have millisecond precision; successive resolution and report
    # submissions can legitimately have the same stored millisecond.
    return submitted_at >= resolved_at if previous_entry.get('status_updated_at') else submitted_at > resolved_at


def claim_receipt_recovery(report_id, entry_index, entry):
    """Reserve one replacement per issue, including across legacy duplicate reports."""
    issue = receipt_issue_key(entry)
    current = reports_col.find_one({'_id': report_id})
    # Legacy records may predate database claims. Preserve their in-flight and
    # completed placements rather than issuing another replacement from a copy.
    for report in reports_col.find({'entries.phone': {'$in': receipt_phone_variants(entry['phone'])}}):
        for index, previous in enumerate(report.get('entries', [])):
            if report['_id'] == report_id and index == entry_index:
                continue
            if receipt_issue_key(previous) != issue:
                continue
            if is_new_issue_after_closure(current, report, previous):
                continue
            if (previous.get('placement_state') in ('placing', 'attention', 'placed')
                    or previous.get('order_id') or previous.get('order_ids')
                    or db['orders'].find_one({'admin_payment_override.key': f'{report["_id"]}:{index}'})):
                raise ValueError('This issue already has a replacement in progress or recorded. No second order was submitted.')
    claims = reports_col.database['failed_receipt_recovery_claims']
    claim = {'_id': issue, 'report_id': report_id, 'entry_index': entry_index,
             'claimed_at': datetime.now(timezone.utc)}
    try:
        claims.insert_one(claim)
    except DuplicateKeyError:
        previous_claim = claims.find_one({'_id': issue})
        previous_report = reports_col.find_one({'_id': (previous_claim or {}).get('report_id')})
        previous_index = (previous_claim or {}).get('entry_index', -1)
        if previous_report and 0 <= previous_index < len(previous_report.get('entries', [])):
            if is_new_issue_after_closure(current, previous_report, previous_report['entries'][previous_index]):
                if claims.replace_one(previous_claim, claim).matched_count:
                    return
        raise ValueError('This issue already has a replacement in progress or recorded. No second order was submitted.') from None


def duplicate_receipt_response():
    return jsonify(success=False, code='duplicate_receipt', message=DUPLICATE_MESSAGE), 409


def receipt_retry_response(existing, report):
    identity = lambda rows: [tuple(row.get(key) for key in ('phone', 'service_id', 'offer_index', 'offer')) for row in rows]
    if (identity(existing.get('entries', [])) != identity(report['entries'])
            or any(existing.get(key) != report.get(key) for key in ('source', 'store_slug'))
            or existing.get('report_type', 'no_receipt') != report['report_type']):
        return jsonify(success=False, message='Please refresh before submitting a new report.'), 409
    return jsonify(success=True, message=f'{len(report["entries"])} receipt report(s) sent to admin.'), 201


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
    report_type = data.get('report_type', 'no_receipt')
    if report_type not in ('no_receipt', 'delivery_issue'):
        return jsonify(success=False, message='Select a valid report type.'), 400
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
              'source': source, 'report_type': report_type, 'submitted_at': datetime.now(timezone.utc),
              'customer_name': user_name(customer) if customer else 'Guest buyer',
              'customer_id': customer.get('_id'),
              'store_name': (store or {}).get('name', ''), 'store_slug': (store or {}).get('slug', ''),
              'store_owner_name': user_name(owner) if store else '',
              'store_owner_id': (store or {}).get('owner_id')}
    try:
        # Preserve safe retries after a lost response, without creating a report twice.
        existing = reports_col.find_one({'_id': submission_id})
        if existing:
            return receipt_retry_response(existing, report)
        issue_keys = [receipt_issue_key(entry) for entry in clean]
        if len(set(issue_keys)) != len(issue_keys):
            return duplicate_receipt_response()
        # Older reports have no duplicate_keys field; check their entries as well.
        # Match within each entry so a phone on one row cannot match another service.
        phones = list({phone for entry in clean for phone in receipt_phone_variants(entry['phone'])})
        keys = set(issue_keys)
        for previous in reports_col.find({'entries.phone': {'$in': phones}}, {'entries': 1, 'duplicate_keys': 1}):
            if any(receipt_issue_key(entry) in keys and entry.get('status') not in CLOSED_RECEIPT_STATUSES
                   for entry in previous.get('entries', []) if entry.get('phone')):
                return duplicate_receipt_response()
            # Release reservations left by reports closed before this update.
            # Compare the entire entries snapshot so a concurrent reopen cannot
            # lose its reservation.
            active_keys = list(dict.fromkeys(receipt_issue_key(entry) for entry in previous.get('entries', [])
                                            if entry.get('status') not in CLOSED_RECEIPT_STATUSES))
            if 'duplicate_keys' in previous:
                change = ({'$set': {'duplicate_keys': active_keys}} if active_keys
                          else {'$unset': {'duplicate_keys': ''}})
                result = reports_col.update_one({'_id': previous['_id'], 'entries': previous['entries']}, change)
                if not result.matched_count:
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
    type_filter = request.args.get('report_type', '')
    if type_filter not in ('no_receipt', 'delivery_issue'):
        type_filter = ''
    type_query = {}
    if type_filter == 'no_receipt':
        type_query = {'$or': [{'report_type': 'no_receipt'}, {'report_type': {'$exists': False}}]}
    elif type_filter:
        type_query = {'report_type': type_filter}
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    service_filter = request.args.get('service', '').strip()
    phone_filter = request.args.get('phone', '').strip()
    phone = normalized_receipt_phone(phone_filter)
    filter_error = ''
    base_query = dict(type_query)
    dates = {}
    try:
        if date_from:
            dates['$gte'] = datetime.strptime(date_from, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if date_to:
            dates['$lt'] = datetime.strptime(date_to, '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1)
        if date_from and date_to and date_from > date_to:
            raise ValueError()
    except (ValueError, OverflowError):
        filter_error = 'Enter a valid date range. The start date must be on or before the end date.'
    if dates:
        base_query['submitted_at'] = dates
    entry_query = {}
    if service_filter:
        entry_query['service_name'] = service_filter
    if phone_filter:
        if not re.fullmatch(r'[0-9]{1,10}', phone):
            filter_error = 'Enter a valid phone number or part of a phone number.'
        entry_query['phone'] = ({'$in': receipt_phone_variants(phone)} if len(phone) == 10
                                else {'$regex': re.escape(phone)})
    if filter_error:
        base_query['_id'] = {'$in': []}
    count_query = dict(base_query)
    if entry_query:
        count_query['entries'] = {'$elemMatch': entry_query}
    total_batches = reports_col.count_documents(count_query)
    selected_entries = dict(entry_query)
    if status_filter:
        selected_entries['status'] = ({'$nin': ['resolved', 'declined', 'rejected', 'cancelled']}
                                      if status_filter == 'unresolved' else status_filter)
    query = dict(base_query)
    if selected_entries:
        query['entries'] = {'$elemMatch': selected_entries}
    filtered_batches = reports_col.count_documents(query)
    pages = max(1, (filtered_batches + 19) // 20)
    page = min(page, pages)
    totals = list(reports_col.aggregate([{'$match': base_query}, {'$unwind': '$entries'},
        {'$match': {'entries.' + key: value for key, value in entry_query.items()}}, {'$group': {
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
            entry['visible'] = (not status_filter or entry['status'] == status_filter
                                or (status_filter == 'unresolved' and entry['status'] not in ('resolved', 'declined', 'rejected', 'cancelled')))
            entry['visible'] = (entry['visible']
                                and (not service_filter or entry.get('service_name') == service_filter)
                                and (not phone_filter or (normalized_receipt_phone(entry.get('phone')) == phone
                                     if len(phone) == 10 else phone in normalized_receipt_phone(entry.get('phone')))))
    def filter_url(**changes):
        filters = dict(status=status_filter, report_type=type_filter, date_from=date_from,
                       date_to=date_to, service=service_filter, phone=phone_filter)
        filters.update(changes)
        return url_for('failed_receipts.admin_page', **{key: value for key, value in filters.items() if value != ''})
    service_options = sorted({name for name in reports_col.distinct('entries.service_name') if isinstance(name, str) and name})
    return render_template('admin_failed_receipts.html', reports=reports, total=counts['total'], resolved=counts['resolved'],
                           unresolved=counts['total'] - sum(counts[key] for key in ('resolved', 'declined', 'rejected', 'cancelled')),
                           declined=counts['declined'], rejected=counts['rejected'], cancelled=counts['cancelled'], status_filter=status_filter,
                           total_batches=total_batches, page=page, pages=pages, type_filter=type_filter,
                           date_from=date_from, date_to=date_to, service_filter=service_filter,
                           phone_filter=phone_filter, service_options=service_options,
                           filter_url=filter_url, filter_error=filter_error), 400 if filter_error else 200


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
receipt_recovery_module.register_receipt_recovery(failed_receipts_bp, reports_col,
    claim_issue=claim_receipt_recovery, update_entry=update_receipt_entry)
