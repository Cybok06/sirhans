"""Adapt complaints to the existing admin recovery flow without duplicating checkout."""
import secrets
import re
from bson import ObjectId
from flask import session

from db import db
from receipt_recovery import register_receipt_recovery, _normalized_value


def _linked_order(complaint):
    ref = complaint.get('order_ref') or {}
    if isinstance(ref, dict):
        oid = ref.get('_id')
        if ObjectId.is_valid(str(oid)):
            order = db['orders'].find_one({'_id': ObjectId(str(oid))})
            if order:
                return order
        for key in ('order_id', 'order_no'):
            if ref.get(key):
                order = db['orders'].find_one({key: ref[key]})
                if order:
                    return order
    number = complaint.get('order_number_provided')
    if number:
        matches = list(db['orders'].find({'$or': [{'order_id': number}, {'order_no': number}]}).limit(2))
        if len(matches) == 1:
            return matches[0]
    return None


def complaint_report(complaint):
    order = _linked_order(complaint) or {}
    store_report = complaint.get('complaint_type') == 'store_report'
    slug = order.get('store_slug') or complaint.get('store_slug')
    recorded_source = complaint.get('source') or complaint.get('origin')
    if not isinstance(recorded_source, str):
        recorded_source = ''
    error = None
    if slug:
        source = 'store'
        if order.get('store_slug') and complaint.get('store_slug') and order['store_slug'] != complaint['store_slug']:
            error = 'The complaint and linked order refer to different stores. Review it manually.'
    elif (order.get('channel') == 'public_web' or order.get('paid_from') == 'public_paystack'
          or recorded_source in {'index', 'index_page', 'public_web'}):
        source = 'index'
    elif (order.get('user_id') and order.get('paid_from') in {'wallet', 'admin_override'}
          or order.get('channel') == 'customer_dashboard'
          or recorded_source in {'customer', 'customer_dashboard'}):
        source = 'customer'
    else:
        source = 'unknown'
        error = 'The complaint origin could not be identified. Link its original order before placing a replacement.'
    items = order.get('items') or []
    phone = complaint.get('reporter_phone') or complaint.get('recipient_phone') or (complaint.get('whatsapp') if store_report else None)
    candidates = items
    if phone:
        def phone_key(value):
            digits = re.sub(r'\D', '', str(value or ''))
            return '0' + digits[3:] if digits.startswith('233') and len(digits) == 12 else digits
        candidates = [item for item in items if phone_key(item.get('phone')) == phone_key(phone)]
        if items and not candidates:
            error = 'The recipient number does not match the linked order. Review it manually.'
    # The existing complaint form records the first purchased line. New records
    # also retain its phone, service ID and actual offer value explicitly.
    item = candidates[0] if candidates else {}
    customer_id = order.get('user_id') or (complaint.get('user_id') if not store_report else None)
    if ObjectId.is_valid(str(customer_id)):
        customer_id = ObjectId(str(customer_id))
    if not store_report and order.get('user_id') and complaint.get('user_id') and str(order['user_id']) != str(complaint['user_id']):
        error = 'The linked order belongs to another account. Review this complaint manually.'
    phone = phone or item.get('phone')
    if not phone:
        error = error or 'The original recipient phone number is missing. Review this complaint manually.'
    recovery = complaint.get('order_recovery') or {}
    value = complaint.get('offer_value') or item.get('value_obj')
    if not value and isinstance(item.get('value'), (dict, str)):
        parsed = _normalized_value(item['value'])
        value = parsed if isinstance(parsed, dict) else None
    entry = {**recovery, 'phone': phone, 'service_id': str(complaint.get('service_id') or item.get('serviceId') or ''),
             'service_name': complaint.get('service_name') or item.get('serviceName'),
             'offer': complaint.get('offer') or item.get('value'), 'offer_value': value,
             'complaint_origin': True, 'status': 'resolved' if complaint.get('status') == 'resolved' else 'unresolved'}
    return {'_id': str(complaint['_id']), 'entries': [entry], 'source': source, 'store_slug': slug,
            'customer_id': customer_id, 'recovery_error': error}


class ComplaintRecoveryCollection:
    """Keep locks/results on the original complaint, never in receipt reports."""
    def __init__(self, collection):
        self.collection = collection

    def _query(self, query):
        result = {}
        for key, value in query.items():
            if key == '_id':
                value = ObjectId(value) if ObjectId.is_valid(str(value)) else None
            elif key.startswith('entries.0.'):
                field = key[len('entries.0.'):]
                key = 'status' if field == 'status' else 'order_recovery.' + field
            result[key] = value
        result['$or'] = [{'complaint_type': {'$ne': 'store_report'}}, {'forwarded_to_admin': True}]
        return result

    def _update(self, update):
        result = {}
        for operation, fields in update.items():
            mapped = {}
            for key, value in fields.items():
                if key.startswith('entries.0.'):
                    field = key[len('entries.0.'):]
                    key = 'status' if field == 'status' else 'order_recovery.' + field
                    if field == 'status' and value == 'unresolved':
                        value = 'pending'
                mapped[key] = value
            result[operation] = mapped
        return result

    def find_one(self, query):
        complaint = self.collection.find_one(self._query(query))
        return complaint_report(complaint) if complaint else None

    def update_one(self, query, update):
        return self.collection.update_one(self._query(query), self._update(update))

    def find_one_and_update(self, query, update, **kwargs):
        complaint = self.collection.find_one_and_update(self._query(query), self._update(update), **kwargs)
        return complaint_report(complaint) if complaint else None


def register_complaint_recovery(bp, collection):
    @bp.app_context_processor
    def recovery_csrf():
        if not session.get('receipt_csrf'):
            session['receipt_csrf'] = secrets.token_urlsafe(32)
        return {'receipt_csrf': session['receipt_csrf']}

    register_receipt_recovery(bp, ComplaintRecoveryCollection(collection),
        route_root='/admin/complaints/order-recovery', return_endpoint='admin_complaints.admin_view_complaints',
        key_prefix='complaint:', reason='Customer complaint', return_label='Customer Complaints', include_status=False)
