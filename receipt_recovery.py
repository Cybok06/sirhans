"""Admin-only recovery. Public checkout endpoints never accept an override flag."""
import secrets
import re
import json
import ast
from decimal import Decimal
from bson import ObjectId
from datetime import datetime, timezone

from flask import abort, current_app, flash, jsonify, redirect, request, session, url_for
from pymongo import ReturnDocument

from db import db


def _now():
    return datetime.now(timezone.utc)


def _authorize(csrf=False):
    if session.get('role') != 'admin' or not session.get('user_id'):
        abort(403)
    if csrf:
        data = request.get_json(silent=True) if request.is_json else request.form
        if request.is_json and not isinstance(data, dict):
            abort(403)
        token = (data or {}).get('csrf_token')
        expected = session.get('receipt_csrf', '')
        if not isinstance(token, str) or not token or not secrets.compare_digest(token, expected):
            abort(403)


def _key(report_id, entry_index):
    return f'{report_id}:{entry_index}'


def _source_context(report):
    if report.get('recovery_error'):
        raise ValueError(report['recovery_error'])
    if report.get('source') == 'customer':
        if not ObjectId.is_valid(str(report.get('customer_id'))):
            raise ValueError('The original customer account is unavailable.')
        import customer_dashboard
        return customer_dashboard, None, customer_dashboard.load_customer_services(report['customer_id'])
    if report.get('source') == 'index':
        import index
        services, _ = index.load_services_for_landing()
        return index, None, services
    if report.get('source') == 'store':
        from routes import store_page
        store = db['stores'].find_one({'slug': report.get('store_slug'), 'status': {'$regex': '^published$', '$options': 'i'}})
        if not store:
            raise ValueError('The original store is unavailable or unpublished.')
        return store_page, store, store_page.load_priced_store_services(store)
    raise ValueError('This report has no valid source page.')


def _build_cart(entry, services):
    """Match a stable service and offer, never an old list position."""
    complaint = entry.get('complaint_origin')
    if not entry.get('service_id') and not complaint:
        raise ValueError('This older report has no service selection. Handle it manually.')
    services = [service for service in services if (
        str(service.get('_id_str') or service.get('_id')) == entry['service_id'] if entry.get('service_id')
        else str(service.get('name') or '').strip().casefold() == str(entry.get('service_name') or '').strip().casefold())]
    if len(services) != 1:
        raise ValueError('The reported service is no longer available on this page.')
    service = services[0]
    offers = service.get('offers') or []
    if complaint and entry.get('offer_value'):
        matches = [offer for offer in offers if _normalized_value(offer.get('value')) == _normalized_value(entry['offer_value'])]
    elif complaint:
        matches = [offer for offer in offers if _offer_label_key(offer.get('value_text') or offer.get('value') or 'Offer') == _offer_label_key(entry.get('offer'))]
    else:
        matches = [offer for offer in offers if str(offer.get('value_text') or offer.get('value') or 'Offer') == entry.get('offer')]
        if entry.get('offer_value') is not None:
            matches = [offer for offer in matches if offer.get('value') == entry['offer_value']]
    if len(matches) != 1:
        raise ValueError('The reported offer is unavailable or ambiguous. Handle it manually.')
    offer = matches[0]
    phone = re.sub(r'[\s+()-]', '', str(entry.get('phone') or ''))
    if re.fullmatch(r'233[0-9]{9}', phone):
        phone = '0' + phone[3:]
    if not re.fullmatch(r'0[0-9]{9}', phone):
        raise ValueError('The complaint has no valid recipient phone number. Handle it manually.')
    return [{'serviceId': str(service.get('_id_str') or service.get('_id')), 'serviceName': service.get('name'),
             'phone': phone, 'value': offer.get('value_text') or entry['offer'],
             'value_text': offer.get('value_text') or entry['offer'], 'value_obj': offer.get('value'),
             'base_amount': offer.get('amount'), 'amount': offer.get('total', offer.get('amount')),
             'service_category': service.get('service_category'), 'service_network': service.get('service_network'),
             'network': service.get('network'), 'unit': service.get('unit')}]


def _checkout(module, store, cart, override):
    """The same source-page order creation and checkout.py provider worker."""
    if hasattr(module, 'place_customer_order_from_admin'):
        return module.place_customer_order_from_admin(cart, override)
    if store:
        return module.place_store_order({
            'store_doc': store, 'slug': store['slug'], 'cart': cart,
            'payment_status': 'admin_override', 'paid_from': 'admin_override',
            'payment_reference': 'receipt:' + override['key'],
            'admin_payment_override': override, 'user_id': override.get('customer_id'),
            'source': {'store_checkout': True, 'receipt_recovery': override['key']},
        }, channel='store_checkout')
    server_cart, total = module._reprice_public_cart(cart)
    if total <= 0:
        raise ValueError('The order total must be greater than zero.')
    return module._create_public_order_from_verified_payment(
        'receipt:' + override['key'], {}, server_cart, total,
        payment_status='admin_override', admin_override=override,
    ), 200


def _normalized_value(value):
    if isinstance(value, str) and value.strip().startswith('{'):
        try:
            return json.loads(value)
        except ValueError:
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError):
                pass
    return value


def _offer_label_key(value):
    text = re.sub(r'\s+', '', str(value or '').casefold())
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(gb|mb)', text)
    if match:
        return Decimal(match[1]) * (1000 if match[2] == 'gb' else 1)
    return text


def _order_outcome(orders):
    if not orders:
        return False, 'No order record was found. Review this placement before attempting another order.'
    for order in orders:
        if str(order.get('status', '')).lower() in {'failed', 'skipped', 'cancelled', 'canceled'}:
            return False, 'The order was created but was not accepted. Review the linked order; it will not be submitted twice.'
        for line in order.get('items') or []:
            api = str(line.get('api_status') or '').lower()
            status = str(line.get('line_status') or '').lower()
            if status.startswith(('failed', 'skipped', 'cancel')) or api in {'failed', 'review_required', 'submitting', 'payment_pending'}:
                return False, 'Order submission failed or needs review. Check the linked order before taking further action.'
    return True, 'Order placed successfully. Delivery continues through normal order processing.'


def _credit_recovery_store(store_slug, orders):
    from routes import store_page
    return store_page._credit_paid_store_order_profits(store_slug, orders, 'admin_override')


def register_receipt_recovery(bp, reports_col, *, route_root='/admin/failed-receipts',
                              return_endpoint='failed_receipts.admin_page', key_prefix='',
                              reason='Failed Receipt / No Receipt', return_label='Failed/No receipt', include_status=True):
    def operation_key(report_id, entry_index):
        return key_prefix + _key(report_id, entry_index)

    def get_entry(report_id, entry_index):
        report = reports_col.find_one({'_id': report_id})
        if not report or entry_index < 0 or entry_index >= len(report.get('entries') or []):
            abort(404)
        return report, report['entries'][entry_index]

    def reconcile(report_id, entry_index):
        key = operation_key(report_id, entry_index)
        orders = list(db['orders'].find({'admin_payment_override.key': key}))
        if not orders:
            return None
        success, message = _order_outcome(orders)
        order_ids = [order['order_id'] for order in orders]
        prefix = f'entries.{entry_index}.'
        reports_col.update_one({'_id': report_id}, {'$set': {
            prefix + 'order_ids': order_ids, prefix + 'order_id': order_ids[0],
            prefix + 'placement_state': 'placed' if success else 'attention',
            prefix + 'status': 'resolved' if success else 'unresolved',
            prefix + 'placement_message': message, prefix + 'status_updated_at': _now(),
        }})
        # A crash between order insertion and store wallet credit is recoverable;
        # the existing helper checks credited_order_ids and never credits twice.
        store_slug = orders[0].get('store_slug')
        if store_slug:
            try:
                _credit_recovery_store(store_slug, orders)
            except Exception:
                current_app.logger.exception('Receipt recovery store credit needs review')
                message += ' Store wallet credit needs review.'
                reports_col.update_one({'_id': report_id}, {'$set': {prefix + 'placement_message': message,
                    prefix + 'placement_state': 'attention', prefix + 'status': 'unresolved'}})
                success = False
        return {'success': success, 'message': message, 'order_id': order_ids[0], 'order_ids': order_ids,
                'return_url': url_for(return_endpoint)}

    def update_status(report_id, entry_index):
        _authorize(csrf=True)
        report, entry = get_entry(report_id, entry_index)
        status = request.form.get('status')
        if status not in ('resolved', 'unresolved'):
            abort(400)
        prefix = f'entries.{entry_index}.'
        updated = reports_col.update_one({'_id': report_id, prefix + 'placement_state': {'$ne': 'placing'}}, {'$set': {
            prefix + 'status': status, prefix + 'status_updated_at': _now(), prefix + 'status_updated_by': session['user_id']}})
        if not updated.matched_count:
            flash('Placement is still running. Wait before changing its status.', 'warning')
        return redirect(url_for(return_endpoint))

    if include_status:
        bp.add_url_rule(f'{route_root}/<report_id>/<int:entry_index>/status', 'update_status', update_status, methods=['POST'])

    @bp.post(f'{route_root}/<report_id>/<int:entry_index>/start')
    def start_placement(report_id, entry_index):
        _authorize(csrf=True)
        report, entry = get_entry(report_id, entry_index)
        key = operation_key(report_id, entry_index)
        existing = reconcile(report_id, entry_index)
        if existing:
            flash(existing['message'], 'success' if existing['success'] else 'warning')
            return redirect(url_for(return_endpoint))
        if entry.get('status') == 'resolved':
            flash('This entry is already resolved. Mark it Unresolved first if it still needs an order.', 'warning')
            return redirect(url_for(return_endpoint))
        tickets = session.get('receipt_recovery_tickets', {})
        tickets[key] = {'token': secrets.token_urlsafe(32), 'admin_id': session['user_id']}
        session['receipt_recovery_tickets'] = dict(list(tickets.items())[-8:])
        return redirect(url_for(bp.name + '.recovery_source', report_id=report_id, entry_index=entry_index))

    @bp.get(f'{route_root}/<report_id>/<int:entry_index>/source')
    def recovery_source(report_id, entry_index):
        _authorize()
        report, entry = get_entry(report_id, entry_index)
        ticket = session.get('receipt_recovery_tickets', {}).get(operation_key(report_id, entry_index))
        if not ticket or ticket['admin_id'] != session['user_id']:
            abort(403)
        try:
            module, store, services = _source_context(report)
            cart = _build_cart(entry, services)
        except ValueError as exc:
            flash(str(exc), 'warning')
            return redirect(url_for(return_endpoint))
        recovery = {'cart': cart, 'token': ticket['token'], 'csrf_token': session['receipt_csrf'],
                    'place_url': url_for(bp.name + '.place_receipt_order', report_id=report_id, entry_index=entry_index),
                    'return_url': url_for(return_endpoint), 'return_label': return_label}
        # Same-origin render of the actual source template keeps the admin session
        # intact even when public stores normally redirect to a different domain.
        if report.get('source') == 'customer':
            return module.render_customer_dashboard(report['customer_id'], services=services, receipt_recovery=recovery)
        if store:
            return module.render_store_page(store, services=services, receipt_recovery=recovery)
        return module.render_index_page(services=services, receipt_recovery=recovery)

    @bp.post(f'{route_root}/<report_id>/<int:entry_index>/place')
    def place_receipt_order(report_id, entry_index):
        _authorize(csrf=True)
        data = request.get_json(silent=True) or {}
        key = operation_key(report_id, entry_index)
        ticket = session.get('receipt_recovery_tickets', {}).get(key)
        token = data.get('token')
        if (not ticket or ticket['admin_id'] != session['user_id'] or not isinstance(token, str)
                or not secrets.compare_digest(token, ticket['token'])):
            abort(403)
        report, entry = get_entry(report_id, entry_index)
        existing = reconcile(report_id, entry_index)
        if existing:
            return jsonify(existing)
        prefix = f'entries.{entry_index}.'
        if entry.get('placement_state') in {'placing', 'attention', 'placed'}:
            return jsonify(success=False, message='Placement is running or needs review. No second order was submitted.',
                           return_url=url_for(return_endpoint)), 409
        try:
            module, store, services = _source_context(report)
            cart = _build_cart(entry, services)
            # Run normal customer validation before taking the placement lock.
            if report.get('source') == 'customer':
                cart, total = module.prepare_customer_recovery_cart(cart, report['customer_id'])
            elif store:
                cart, total = module._server_reprice_store_cart(store, cart)
            else:
                cart, total = module._reprice_public_cart(cart)
            if total <= 0:
                raise ValueError('The order total must be greater than zero.')
        except ValueError as exc:
            reports_col.update_one({'_id': report_id}, {'$set': {prefix + 'placement_message': str(exc)}})
            return jsonify(success=False, message=str(exc), return_url=url_for(return_endpoint)), 400
        claimed = reports_col.find_one_and_update({
            '_id': report_id, prefix + 'status': {'$ne': 'resolved'},
            prefix + 'placement_state': {'$nin': ['placing', 'placed', 'attention']},
            prefix + 'order_id': {'$exists': False},
        }, {'$set': {prefix + 'placement_state': 'placing', prefix + 'placement_started_at': _now(),
                    prefix + 'placement_admin_id': session['user_id'], prefix + 'placement_message': 'Placing order…'}},
           return_document=ReturnDocument.AFTER)
        if not claimed:
            return jsonify(success=False, message='This entry is already being handled.', return_url=url_for(return_endpoint)), 409
        override = {'key': key, 'report_id': report_id, 'entry_index': entry_index,
                    'admin_id': session['user_id'], 'customer_id': report.get('customer_id'),
                    'authorized_at': _now(), 'reason': reason, 'amount_collected': 0.0}
        try:
            result, status_code = _checkout(module, store, cart, override)
            existing = reconcile(report_id, entry_index)
            if existing:
                return jsonify(existing)
            message = result.get('message') or 'Order placement needs review.'
        except Exception:
            current_app.logger.exception('Receipt placement interrupted: %s', key)
            existing = reconcile(report_id, entry_index)
            if existing:
                return jsonify(existing)
            message = 'Placement was interrupted. Review before attempting another order; it will not be automatically submitted twice.'
        # Never release a lock after side effects may have started, including SMS.
        reports_col.update_one({'_id': report_id}, {'$set': {prefix + 'placement_state': 'attention',
            prefix + 'status': 'unresolved', prefix + 'placement_message': message}})
        return jsonify(success=False, message=message, return_url=url_for(return_endpoint)), 409
