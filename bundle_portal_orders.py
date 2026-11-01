"""Order persistence and authenticated Bundle Portal delivery callbacks."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
import logging
import re
import threading
import uuid

from flask import Blueprint, current_app, jsonify, request, session, flash, redirect, url_for
from bson import ObjectId
from pymongo import ReturnDocument, timeout
from pymongo.errors import PyMongoError

from bundle_portal import PROVIDERS, call, submit, canonical_network

bundle_portal_bp = Blueprint("bundle_portal", __name__)
FINAL_LINES = {"delivered", "completed", "refunded"}
WEBHOOK_PROVIDERS = set(PROVIDERS) | {'bundleportal'}
EVENTS = {'order.completed': 'completed', 'order.failed': 'failed',
          'order.cancelled': 'cancelled', 'order.refunded': 'refunded'}
logger = logging.getLogger(__name__)


def _secret():
    return (os.getenv('BUNDLE_PORTAL_WEBHOOK_SECRET') or os.getenv('BUNDLEPORTAL_WEBHOOK_SECRET') or '').strip()


def _item_network(item):
    if item.get('provider') in PROVIDERS:
        return PROVIDERS[item['provider']]
    if item.get('provider') == 'bundleportal':
        network = item.get('provider_network') or item.get('network')
        return canonical_network(network.lower()) if isinstance(network, str) else None
    return None


def _settled_at(payload):
    value = payload.get('settled_at')
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError('Invalid settlement timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _valid_payload(payload):
    if not isinstance(payload, dict):
        return False
    event = payload.get('event')
    if (not isinstance(event, str) or EVENTS.get(event) != payload.get('status') or event not in EVENTS
            or not isinstance(payload.get('order_id'), str) or not 1 <= len(payload['order_id']) <= 80
            or not isinstance(payload.get('network'), str)
            or canonical_network(payload['network']) not in set(PROVIDERS.values())
            or not isinstance(payload.get('recipient'), str) or not re.fullmatch(r'0[0-9]{9}', payload['recipient'])):
        return False
    try:
        _settled_at(payload)
    except (ValueError, TypeError):
        return False
    return True


def _find_item(database, payload):
    order = database['orders'].find_one({'items': {'$elemMatch': {
        'provider_request_order_id': payload['order_id'], 'provider': {'$in': list(WEBHOOK_PROVIDERS)},
    }}})
    if not order:
        return None, None
    item = next((item for item in order.get('items', []) if item.get('provider_request_order_id') == payload['order_id']
                 and item.get('provider') in WEBHOOK_PROVIDERS), None)
    return order, item


def _matches(item, payload):
    return bool(item and _item_network(item) == canonical_network(payload['network'])
                and item.get('phone') == payload['recipient'])


def ensure_webhook_indexes(database):
    try:
        with timeout(5):
            database['orders'].create_index('items.provider_request_order_id')
            database['bundleportal_events'].create_index([('payload.order_id', 1), ('received_at', -1)])
            database['bundleportal_events'].create_index([('processing_state', 1), ('next_attempt_at', 1)])
    except PyMongoError:
        logger.exception('BundlePortal webhook indexes could not be created')


def refresh_order(collection, order_id):
    # Compare the snapshot to avoid overwriting another line's concurrent result.
    for _ in range(5):
        order = collection.find_one({"order_id": order_id})
        if not order:
            return
        items = order.get("items") or []
        statuses = [i.get("line_status") for i in items if not str(i.get("line_status", "")).startswith("skipped")]
        if not statuses:
            return
        if all(s in {"delivered", "completed"} for s in statuses):
            status = "delivered"
        elif all(s == "refunded" for s in statuses):
            status = "refunded"
        elif all(s in {"failed", "refunded"} for s in statuses):
            status = "failed"
        else:
            status = "processing"
        if order.get("status") == "completed":
            collection.update_one({"_id": order["_id"], "status": "completed"},
                                  {"$set": {"bundleportal_parent_refresh_pending": False}})
            return
        fields = {"status": status, "updated_at": datetime.utcnow(),
                  "bundleportal_parent_refresh_pending": False,
                  "profit_amount_total": round(sum(float(i.get("profit_amount") or 0) for i in items), 2)}
        if status == "delivered" and not order.get("delivered_at"):
            fields["delivered_at"] = datetime.utcnow()
        result = collection.update_one(
            {"_id": order["_id"], "items": items, "status": order.get("status")},
            {"$set": fields},
        )
        if result.matched_count:
            return


def invalidate_orders():
    # Sirhans order data is read live; no nagonu cache dependency.
    pass


def apply_status_event(collection, order, item, payload):
    """Apply a verified stored event atomically, shared by webhook and reconciliation."""
    event = payload.get("event")
    if (not _valid_payload(payload)
            or payload.get("order_id") != item.get("provider_request_order_id")
            or canonical_network(payload.get("network")) != _item_network(item)
            or payload.get("recipient") != item.get("phone")):
        return False
    target = "delivered" if event == "order.completed" else "failed"
    blocked = list(FINAL_LINES | {"failed"})
    if event == "order.refunded":
        blocked = ["refunded", "completed"]
    guards = [{'$or': [{'line_status': {'$nin': blocked}},
                       {'line_status': 'failed', 'provider_status_payload': {'$exists': False}}]}]
    settled_at = _settled_at(payload)
    if settled_at:
        guards.append({'$or': [{'provider_settled_at': {'$exists': False}}, {'provider_settled_at': {'$lte': settled_at}}]})
    fields = {
        "items.$.line_status": target,
        "items.$.api_status": "success" if target == "delivered" else "failed",
        "items.$.provider_status": payload["status"],
        "items.$.provider_reference": payload.get("reference"),
        "items.$.provider_status_payload": payload,
        "items.$.provider_status_checked_at": datetime.utcnow(),
        "items.$.refund_required": target == "failed",
        "items.$.provider_failure_reason": payload.get('failure_reason'),
        "bundleportal_parent_refresh_pending": True,
        "updated_at": datetime.utcnow(),
    }
    if settled_at:
        fields['items.$.provider_settled_at'] = settled_at
        if target == 'delivered':
            fields['items.$.delivered_at'] = settled_at
    result = collection.update_one({"_id": order["_id"], "items": {"$elemMatch": {
        "provider_request_order_id": payload["order_id"], "provider": item["provider"],
        '$and': guards,
        "provider_status_payload": {"$ne": payload},
    }}}, {'$set': fields})
    return bool(result.modified_count)


def process_job(collection, order_id, job):
    reference = job["provider_request_order_id"]
    provider = job["provider"]
    selector = {"order_id": order_id, "items": {"$elemMatch": {
        "provider_request_order_id": reference, "provider": provider,
        "api_status": {"$in": ["queued", "submitting", "review_required"]}, "line_status": {"$in": ["pending", "processing"]},
    }}}
    claimed = collection.update_one(selector, {"$set": {
        "items.$.api_status": "bundleportal_submitting", "updated_at": datetime.utcnow(),
    }})
    if not claimed.modified_count:
        return
    payload = submit(provider, job.get("phone"), job.get("package_size_gb") or job.get("bundle_portal_gb_size"), reference,
                     retry_purchase=bool(job.get("retry_purchase")))
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    accepted = payload.get("success") is True
    # A timeout/5xx may follow a charged purchase. Keep it for webhook/manual review.
    uncertain = (payload.get("code") in {"unknown_outcome", "recipient_blocked", "not_allowlisted"}
                 or payload.get("http_status", 0) in {409, 429}
                 or payload.get("http_status", 0) >= 500)
    raw_status = data.get("status")
    line_status = "delivered" if accepted and raw_status == "completed" else "processing"
    if (not accepted and not uncertain) or (accepted and raw_status in {"failed", "cancelled", "refunded"}):
        line_status = "failed"
    fields = {
        "items.$.api_status": "success" if accepted else ("review_required" if uncertain else "failed"),
        "items.$.line_status": line_status,
        "items.$.api_response": payload,
        "items.$.provider_reference": data.get("reference"),
        "items.$.provider_order_id": reference,
        "items.$.provider_status": raw_status,
        "items.$.provider_submission_attempted": bool(payload.get("order_attempted")),
        "items.$.refund_required": line_status == "failed",
        "updated_at": datetime.utcnow(),
    }
    if accepted and data.get("amount") is not None:
        try:
            cost = round(float(data["amount"]), 2)
            price = round(float(job.get("amount") or 0), 2)
            if cost > 0:
                profit = round(price - cost, 2)
                fields.update({"items.$.base_amount": cost, "items.$.provider_package_amount": cost,
                               "items.$.profit_amount": profit,
                               "items.$.profit_percent_used": round(profit / cost * 100, 2)})
        except (ValueError, TypeError):
            pass
    # A fast webhook must never be overwritten by the initial HTTP response.
    collection.update_one({"order_id": order_id, "items": {"$elemMatch": {
        "provider_request_order_id": reference, "provider": provider,
        "api_status": "bundleportal_submitting", "line_status": {"$nin": list(FINAL_LINES | {"failed"})},
    }}}, {"$set": fields})
    refresh_order(collection, order_id)
    invalidate_orders()


def process_webhook_event(database, event_id):
    """Claim a durable delivery. Expired claims are safe to resume after a restart."""
    now, claim = datetime.utcnow(), uuid.uuid4().hex
    events = database['bundleportal_events']
    owned = {'_id': event_id, 'claim_token': claim}
    try:
        with timeout(6):
            event = events.find_one_and_update({'_id': event_id, 'authenticated': True,
                'next_attempt_at': {'$lte': now}, '$or': [
                    {'processing_state': {'$in': ['pending', 'retry', 'awaiting_order', 'awaiting_payment']}},
                    {'processing_state': 'processing', 'lease_until': {'$lte': now}},
                ]}, {'$set': {'processing_state': 'processing', 'claim_token': claim,
                             'lease_until': now + timedelta(seconds=30)}, '$inc': {'processing_attempts': 1}},
                return_document=ReturnDocument.AFTER)
            if not event:
                return 'unchanged'
            payload = event['payload']
            order, item = _find_item(database, payload)
            if not order:
                events.update_one(owned, {'$set': {'processing_state': 'awaiting_order',
                    'next_attempt_at': now + timedelta(seconds=30), 'last_error': 'Order has not been saved yet'},
                    '$unset': {'claim_token': '', 'lease_until': ''}})
                return 'awaiting_order'
            if not _matches(item, payload):
                events.update_one(owned, {'$set': {'processing_state': 'rejected', 'last_error': 'Order details do not match'},
                                         '$unset': {'claim_token': '', 'lease_until': ''}})
                return 'rejected'
            source = order.get('source') if isinstance(order.get('source'), dict) else {}
            if ((order.get('channel') == 'arkesel_ussd' or source.get('provider') == 'arkesel')
                    and str(order.get('payment_status') or '').lower() != 'paid'):
                events.update_one(owned, {'$set': {'processing_state': 'awaiting_payment',
                    'next_attempt_at': now + timedelta(seconds=30), 'last_error': 'Waiting for verified payment'},
                    '$unset': {'claim_token': '', 'lease_until': ''}})
                return 'awaiting_payment'
            changed = apply_status_event(database['orders'], order, item, payload)
            refresh_order(database['orders'], order['order_id'])
            invalidate_orders()
            events.update_one(owned, {'$set': {'processing_state': 'processed', 'processed_at': datetime.utcnow(),
                'local_order_id': order['order_id'], 'line_updated': changed},
                '$unset': {'claim_token': '', 'lease_until': '', 'last_error': ''}})
            return 'processed'
    except Exception:
        logger.exception('BundlePortal delivery processing failed: %s', event_id)
        try:
            with timeout(3):
                events.update_one(owned, {'$set': {'processing_state': 'retry',
                    'next_attempt_at': datetime.utcnow() + timedelta(seconds=30), 'last_error': 'Processing interrupted; retry scheduled'},
                    '$unset': {'claim_token': '', 'lease_until': ''}})
        except PyMongoError:
            pass  # The lease expires; the scheduler will reclaim this delivery.
        return 'retry'


def process_pending_webhook_events(database, limit=100):
    now = datetime.utcnow()
    query = {'authenticated': True, 'next_attempt_at': {'$lte': now}, '$or': [
        {'processing_state': {'$in': ['pending', 'retry', 'awaiting_order', 'awaiting_payment']}},
        {'processing_state': 'processing', 'lease_until': {'$lte': now}},
    ]}
    summary = {'processed': 0, 'awaiting_order': 0, 'awaiting_payment': 0, 'rejected': 0, 'retry': 0, 'unchanged': 0}
    for event in database['bundleportal_events'].find(query, {'_id': 1}).sort('received_at', 1).limit(limit):
        state = process_webhook_event(database, event['_id'])
        summary[state] += 1
    return summary


def dispatch_webhook_event(database, event_id):
    if not current_app.config.get('BUNDLEPORTAL_WEBHOOK_ASYNC', True):
        process_webhook_event(database, event_id)
        return
    try:
        threading.Thread(target=process_webhook_event, args=(database, event_id), daemon=True).start()
    except Exception:
        logger.exception('BundlePortal delivery queued for scheduled processing: %s', event_id)


@bundle_portal_bp.route("/webhooks/bundleportal", methods=["GET", "POST"])
def webhook():
    secret = _secret()
    if request.method == 'GET':
        return jsonify(provider='bundleportal', api_version='v2', configured=bool(secret), status_updates='webhook')
    if not secret:
        return jsonify(success=False, error="Webhook is not configured"), 503
    if request.content_length and request.content_length > 65536:
        return jsonify(success=False, error='Payload too large'), 413
    raw = request.get_data()
    if len(raw) > 65536:
        return jsonify(success=False, error='Payload too large'), 413
    signature = request.headers.get("X-BundlePortal-Signature", "")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return jsonify(success=False, error="Invalid signature"), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(success=False, error="Invalid payload"), 400
    if payload.get('event') == 'webhook.test':
        return jsonify(success=True, test=True, provider='bundleportal')
    if not _valid_payload(payload):
        return jsonify(success=False, error="Invalid order event"), 400
    from db import db
    delivery = request.headers.get('X-BundlePortal-Delivery', '').strip()
    if len(delivery) > 200:
        return jsonify(success=False, error='Invalid delivery ID'), 400
    digest = hashlib.sha256(raw).hexdigest()
    event_id = 'delivery:' + delivery if delivery else digest
    try:
        delivery_attempt = max(1, min(int(request.headers.get('X-BundlePortal-Attempt', '1')), 1000))
    except ValueError:
        delivery_attempt = 1
    try:
        with timeout(6):
            order, item = _find_item(db, payload)
            if order and not _matches(item, payload):
                return jsonify(success=False, error='Order details do not match'), 400
            now = datetime.utcnow()
            db['bundleportal_events'].update_one({'_id': event_id}, {'$setOnInsert': {
                'payload': payload, 'body_sha256': digest, 'delivery_id': delivery or None,
                'authenticated': True, 'received_at': now, 'processing_state': 'pending',
                'next_attempt_at': now, 'processing_attempts': 0},
                '$max': {'delivery_attempt': delivery_attempt}}, upsert=True)
            saved = db['bundleportal_events'].find_one({'_id': event_id}, {'body_sha256': 1, 'processing_state': 1})
            if saved and 'body_sha256' not in saved and event_id == digest:
                # Adopt authenticated events saved by the previous receiver.
                db['bundleportal_events'].update_one({'_id': event_id, 'body_sha256': {'$exists': False}}, {'$set': {
                    'payload': payload, 'body_sha256': digest, 'authenticated': True,
                    'processing_state': 'pending', 'next_attempt_at': now, 'processing_attempts': 0}})
                saved = {'body_sha256': digest, 'processing_state': 'pending'}
            if not saved:
                return jsonify(success=False, error='Callback storage unavailable; retry delivery'), 503
            if saved.get('body_sha256') != digest:
                return jsonify(success=False, error='Delivery ID reused with different content'), 409
    except PyMongoError:
        logger.exception('BundlePortal callback could not be saved')
        return jsonify(success=False, error='Callback storage unavailable; retry delivery'), 503
    if saved.get('processing_state') not in {'processed', 'rejected', 'processing'}:
        dispatch_webhook_event(db, event_id)
    return jsonify(success=True, accepted=True, delivery_id=delivery or digest)


@bundle_portal_bp.route("/admin/services/bundleportal/catalog", methods=["GET"])
def catalog():
    if session.get("role") != "admin":
        return jsonify(success=False, error="Unauthorized"), 401
    network = request.args.get("network", "mtn")
    if network not in PROVIDERS.values():
        return jsonify(success=False, error="Invalid Bundle Portal network"), 400
    payload = call("get_bundles", network=network)
    return jsonify(payload), 200 if payload.get("success") else 502


