from __future__ import annotations

import hashlib
import hmac
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

import requests
from flask import Blueprint, jsonify, request

from checkout import _background_process_providers, jlog
from db import db
from deposit import PAYSTACK_SECRET_KEY as DEPOSIT_PAYSTACK_SECRET_KEY

paystack_webhook_bp = Blueprint("paystack_webhook", __name__)

orders_col = db["orders"]
transactions_col = db["transactions"]
ussd_pending_payments_col = db["ussd_pending_payments"]


def _secret_key() -> str:
    return (
        os.getenv("PAYSTACK_SECRET_KEY")
        or os.getenv("PAYSTACK_SK")
        or DEPOSIT_PAYSTACK_SECRET_KEY
        or ""
    ).strip()


def _verify_signature(raw_body: bytes) -> bool:
    secret = _secret_key()
    signature = (request.headers.get("x-paystack-signature") or "").strip()
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, signature)


def _money_to_pesewas(value: Any) -> int:
    try:
        return int(round(float(value or 0) * 100))
    except Exception:
        return 0


def verify_paystack_transaction(reference: str) -> tuple[bool, Dict[str, Any], str]:
    reference = str(reference or "").strip()
    secret = _secret_key()
    if not reference:
        return False, {}, "Missing payment reference"
    if not secret:
        return False, {}, "Paystack secret key is not configured"
    try:
        resp = requests.get(
            f"https://api.paystack.co/charge/{reference}",
            headers={"Authorization": f"Bearer {secret}"},
            timeout=20,
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:
        return False, {}, f"Paystack verify error: {exc}"

    if resp.status_code >= 400 or not data.get("status"):
        return False, data, data.get("message") or "Paystack verification failed"

    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    status = str(payload.get("status") or "").strip().lower()
    if status != "success":
        return False, payload, payload.get("gateway_response") or f"Payment status is {status or 'unknown'}"
    return True, payload, ""


def _is_terminal_verify_failure(payload: Dict[str, Any], reason: str) -> bool:
    status = str((payload or {}).get("status") or "").strip().lower()
    if status in {"failed", "abandoned", "reversed"}:
        return True
    if status in {"pending", "send_otp", "pay_offline", "processing", "ongoing"}:
        return False

    text = str(reason or "").strip().lower()
    if not text:
        return False
    terminal_fragments = (
        "transaction reference not found",
        "transaction was not completed",
        "low_balance_or_payee_limit_reached_or_not_allowed",
        "unable to perform transaction",
        "an error occurred while processing the request",
    )
    return any(fragment in text for fragment in terminal_fragments)


def mark_arkesel_payment_failed(reference: str, reason: str, payload: Optional[Dict[str, Any]] = None) -> None:
    reference = str(reference or "").strip()
    if not reference:
        return

    now = datetime.utcnow()
    ussd_pending_payments_col.update_many(
        {"$or": [{"payment_reference": reference}, {"paystack_reference": reference}]},
        {
            "$set": {
                "status": "payment_failed",
                "payment_status": "failed",
                "payment_start_error": reason,
                "last_verify_payload": payload or {},
                "last_verify_at": now,
                "updated_at": now,
            }
        },
    )
    orders_col.update_many(
        {"$or": [{"payment_reference": reference}, {"paystack_reference": reference}]},
        {
            "$set": {
                "status": "payment_failed",
                "payment_status": "failed",
                "payment_start_error": reason,
                "last_verify_payload": payload or {},
                "last_verify_at": now,
                "updated_at": now,
            }
        },
    )
    transactions_col.update_many(
        {"payment_reference": reference, "source": "arkesel_ussd"},
        {
            "$set": {
                "status": "failed",
                "payment_status": "failed",
                "payment_start_error": reason,
                "updated_at": now,
            }
        },
    )


def _find_order(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    reference = str(data.get("reference") or "").strip()
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    order_id = str(metadata.get("order_id") or "").strip()

    clauses = []
    if reference:
        clauses.extend([{"payment_reference": reference}, {"paystack_reference": reference}])
    if order_id:
        clauses.append({"order_id": order_id})
    if not clauses:
        return None
    return orders_col.find_one({"$or": clauses})


def _find_pending_payment(reference: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    order_id = str(metadata.get("order_id") or "").strip()
    clauses = []
    if reference:
        clauses.extend([{"payment_reference": reference}, {"paystack_reference": reference}])
    if order_id:
        clauses.append({"order_id": order_id})
    if not clauses:
        return None
    return ussd_pending_payments_col.find_one(
        {"$or": clauses},
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def _release_provider_processing(order_doc: Dict[str, Any], now: datetime) -> bool:
    order_id = order_doc.get("order_id")
    if not order_id:
        return False

    jobs = order_doc.get("pending_provider_jobs") or []
    items = []
    for item in order_doc.get("items") or []:
        item = dict(item)
        if item.get("line_status") == "awaiting_payment":
            item["line_status"] = "processing"
        if item.get("api_status") == "payment_pending":
            item["api_status"] = "submitting"
        if isinstance(item.get("api_response"), dict) and item["api_response"].get("note"):
            item["api_response"]["note"] = "Payment confirmed; submitting directly to provider."
        items.append(item)

    claimed = orders_col.update_one(
        {"_id": order_doc["_id"], "provider_processing_started": {"$ne": True}},
        {
            "$set": {
                "provider_processing_started": True,
                "provider_processing_started_at": now,
                "status": "processing",
                "items": items,
                "updated_at": now,
            }
        },
    )
    if not claimed.modified_count:
        return False

    if jobs:
        try:
            _background_process_providers(order_id, jobs)
        except Exception as exc:
            jlog("paystack_ussd_provider_submit_error", order_id=order_id, error=str(exc))
    return True


def complete_arkesel_ussd_payment(order_doc: Dict[str, Any], data: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not order_doc:
        return {"success": False, "message": "Order not found"}
    if order_doc.get("channel") != "arkesel_ussd":
        return {"success": False, "message": "Not an Arkesel USSD order"}

    paid_amount = int(data.get("amount") or 0)
    expected_amount = _money_to_pesewas(order_doc.get("charged_amount") or order_doc.get("total_amount"))
    currency = str(data.get("currency") or "").upper()
    if currency != "GHS" or paid_amount != expected_amount:
        return {"success": False, "message": "Payment amount or currency mismatch"}

    now = datetime.utcnow()
    reference = str(data.get("reference") or order_doc.get("payment_reference") or "").strip()
    update_fields = {
        "payment_status": "paid",
        "paid_at": now,
        "status": "processing",
        "payment_provider": "paystack",
        "payment_channel": "mobile_money",
        "payment_reference": reference,
        "paystack_reference": reference,
        "updated_at": now,
    }
    if event is not None:
        update_fields["paystack_webhook_event"] = event

    orders_col.update_one({"_id": order_doc["_id"]}, {"$set": update_fields})
    transactions_col.update_many(
        {
            "$or": [
                {"reference": order_doc.get("order_id")},
                {"payment_reference": reference},
            ],
            "source": "arkesel_ussd",
        },
        {
            "$set": {
                "payment_status": "paid",
                "status": "success",
                "payment_reference": reference,
                "paid_at": now,
                "updated_at": now,
            }
        },
    )
    ussd_pending_payments_col.update_many(
        {"$or": [{"payment_reference": reference}, {"paystack_reference": reference}]},
        {
            "$set": {
                "status": "completed",
                "payment_status": "paid",
                "paid_at": now,
                "completed_at": now,
                "updated_at": now,
            }
        },
    )
    db["ussd_sessions"].update_many(
        {"payment_reference": reference},
        {"$set": {"status": "completed", "paid_at": now, "updated_at": now}},
    )

    refreshed = orders_col.find_one({"_id": order_doc["_id"]}) or order_doc
    released = _release_provider_processing(refreshed, now)
    return {"success": True, "released_provider_processing": released}


def _complete_pending_arkesel_ussd_payment(pending_doc: Dict[str, Any], data: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not pending_doc:
        return {"success": False, "message": "Pending payment not found"}

    paid_amount = int(data.get("amount") or 0)
    expected_amount = _money_to_pesewas(pending_doc.get("amount") or pending_doc.get("total_requested"))
    currency = str(data.get("currency") or "").upper()
    if currency != "GHS" or paid_amount != expected_amount:
        return {"success": False, "message": "Payment amount or currency mismatch"}

    order_id = pending_doc.get("order_id")
    reference = str(data.get("reference") or pending_doc.get("payment_reference") or "").strip()
    existing = orders_col.find_one({"order_id": order_id}) if order_id else None
    if existing:
        return complete_arkesel_ussd_payment(existing, data, event=event)

    now = datetime.utcnow()
    claim = ussd_pending_payments_col.update_one(
        {
            "_id": pending_doc["_id"],
            "status": {
                "$in": [
                    "charge_initiating",
                    "awaiting_paystack_otp",
                    "awaiting_payment",
                    "payment_processing",
                    "creating_order",
                    "order_create_failed",
                ]
            },
        },
        {
            "$set": {
                "status": "creating_order",
                "payment_status": "paid",
                "paid_at": now,
                "payment_reference": reference,
                "paystack_reference": reference,
                "paystack_success_data": data,
                "paystack_webhook_event": event,
                "updated_at": now,
            }
        },
    )
    if not claim.modified_count:
        existing = orders_col.find_one({"order_id": order_id}) if order_id else None
        if existing:
            return complete_arkesel_ussd_payment(existing, data, event=event)
        refreshed_pending = ussd_pending_payments_col.find_one({"_id": pending_doc["_id"]}) or pending_doc
        refreshed_status = str(refreshed_pending.get("status") or "").strip().lower()
        if refreshed_status in {"creating_order", "order_create_failed"}:
            try:
                ussd_pending_payments_col.update_one(
                    {"_id": pending_doc["_id"]},
                    {
                        "$set": {
                            "status": "payment_processing",
                            "updated_at": datetime.utcnow(),
                        },
                        "$inc": {"retry_count": 1},
                    },
                )
            except Exception:
                pass
            return _complete_pending_arkesel_ussd_payment(refreshed_pending, data, event=event)
        return {"success": True, "released_provider_processing": False, "pending_already_claimed": True}

    kind = pending_doc.get("kind")
    if kind == "store":
        from routes.store_page import place_store_order

        payload = dict(pending_doc.get("place_payload") or {})
        payload.update(
            {
                "order_id": order_id,
                "payment_status": "paid",
                "paid_from": "paystack_mobile_money",
                "order_status": "pending",
                "defer_provider_processing": False,
                "payment_provider": "paystack",
                "payment_channel": "mobile_money",
                "payment_reference": reference,
                "paystack_reference": reference,
                "charged_amount": round(float(pending_doc.get("amount") or 0), 2),
                "gateway_fee_overage_ghs": round(float(pending_doc.get("gateway_fee_overage_ghs") or 0), 2),
            }
        )
        result, status_code = place_store_order(payload, channel="arkesel_ussd")
        if not result.get("success"):
            ussd_pending_payments_col.update_one(
                {"_id": pending_doc["_id"]},
                {"$set": {"status": "order_create_failed", "order_create_error": result, "updated_at": datetime.utcnow()}},
            )
            return {"success": False, "message": result.get("message") or "Order creation failed", "status_code": status_code}

    elif kind == "public":
        from index import _create_public_order_from_verified_payment

        result = _create_public_order_from_verified_payment(
            reference,
            data,
            pending_doc.get("server_cart") or [],
            float(pending_doc.get("total_requested") or pending_doc.get("amount") or 0),
            order_id_override=order_id,
        )
        if not result.get("success"):
            ussd_pending_payments_col.update_one(
                {"_id": pending_doc["_id"]},
                {"$set": {"status": "order_create_failed", "order_create_error": result, "updated_at": datetime.utcnow()}},
            )
            return {"success": False, "message": result.get("message") or "Order creation failed"}
        orders_col.update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "channel": "arkesel_ussd",
                    "source": pending_doc.get("source") or {},
                    "payment_status": "paid",
                    "payment_provider": "paystack",
                    "payment_channel": "mobile_money",
                    "payment_reference": reference,
                    "paystack_reference": reference,
                    "charged_amount": round(float(pending_doc.get("amount") or 0), 2),
                    "paid_from": "paystack_mobile_money",
                    "paid_at": now,
                    "updated_at": now,
                }
            },
        )
        transactions_col.update_one(
            {"reference": order_id, "source": "arkesel_ussd"},
            {
                "$set": {
                    "user_id": None,
                    "amount": round(float(pending_doc.get("amount") or 0), 2),
                    "reference": order_id,
                    "status": "success",
                    "payment_status": "paid",
                    "type": "debit",
                    "source": "arkesel_ussd",
                    "channel": "arkesel_ussd",
                    "currency": "GHS",
                    "payment_reference": reference,
                    "paid_at": now,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    else:
        return {"success": False, "message": "Unsupported pending payment kind"}

    ussd_pending_payments_col.update_one(
        {"_id": pending_doc["_id"]},
        {"$set": {"status": "completed", "completed_at": datetime.utcnow(), "updated_at": datetime.utcnow()}},
    )
    db["ussd_sessions"].update_many(
        {"payment_reference": reference},
        {"$set": {"status": "completed", "paid_at": now, "updated_at": now}},
    )
    return {"success": True, "released_provider_processing": True, "order_created": True, "order_id": order_id}


def complete_arkesel_ussd_payment_by_reference(reference: str, data: Dict[str, Any], event: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    reference = str(reference or data.get("reference") or "").strip()
    order_doc = _find_order({**data, "reference": reference})
    if order_doc:
        return complete_arkesel_ussd_payment(order_doc, data, event=event)
    pending_doc = _find_pending_payment(reference, data)
    if pending_doc:
        return _complete_pending_arkesel_ussd_payment(pending_doc, data, event=event)
    return {"success": False, "message": "Order not found"}


def reconcile_arkesel_pending_payments(*, reference: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    query: Dict[str, Any] = {
        "channel": "arkesel_ussd",
        "payment_reference": {"$exists": True, "$ne": ""},
        "status": {"$in": ["charge_initiating", "awaiting_payment", "payment_processing", "creating_order", "order_create_failed"]},
    }
    if reference:
        query["$or"] = [{"payment_reference": reference}, {"paystack_reference": reference}]

    scanned = 0
    completed = 0
    still_pending = 0
    marked_failed = 0
    failures: list[Dict[str, Any]] = []

    cursor = ussd_pending_payments_col.find(query).sort("updated_at", 1).limit(max(int(limit or 0), 1))
    for pending_doc in cursor:
        scanned += 1
        payment_reference = str(
            pending_doc.get("payment_reference") or pending_doc.get("paystack_reference") or ""
        ).strip()
        if not payment_reference:
            failures.append({"order_id": pending_doc.get("order_id"), "reason": "missing_reference"})
            continue

        ok, payload, reason = verify_paystack_transaction(payment_reference)
        if ok:
            result = complete_arkesel_ussd_payment_by_reference(payment_reference, payload)
            if result.get("success"):
                completed += 1
            else:
                failures.append(
                    {"order_id": pending_doc.get("order_id"), "reference": payment_reference, "reason": result.get("message")}
                )
            continue

        if _is_terminal_verify_failure(payload if isinstance(payload, dict) else {}, reason):
            mark_arkesel_payment_failed(payment_reference, reason, payload if isinstance(payload, dict) else {})
            marked_failed += 1
            continue

        still_pending += 1
        ussd_pending_payments_col.update_one(
            {"_id": pending_doc["_id"]},
            {
                "$set": {
                    "last_verify_message": reason,
                    "last_verify_payload": payload if isinstance(payload, dict) else {},
                    "last_verify_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    return {
        "success": True,
        "scanned": scanned,
        "completed": completed,
        "still_pending": still_pending,
        "marked_failed": marked_failed,
        "failures": failures,
    }


@paystack_webhook_bp.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    raw_body = request.get_data() or b""
    if not _verify_signature(raw_body):
        jlog("paystack_webhook_invalid_signature")
        return jsonify({"success": False, "message": "Invalid signature"}), 401

    event = request.get_json(silent=True) or {}
    if event.get("event") != "charge.success":
        jlog("paystack_webhook_ignored_event", event=event.get("event"))
        return jsonify({"success": True, "ignored": True}), 200

    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    jlog(
        "paystack_webhook_charge_success",
        reference=str(data.get("reference") or ""),
        status=str(data.get("status") or ""),
        channel=str(data.get("channel") or ""),
    )
    result = complete_arkesel_ussd_payment_by_reference(str(data.get("reference") or ""), data, event=event)
    if result.get("message") == "Order not found":
        jlog("paystack_webhook_order_not_found", reference=str(data.get("reference") or ""))
        return jsonify({"success": True, "ignored": True, "reason": "order_not_found"}), 200
    if result.get("message") == "Not an Arkesel USSD order":
        jlog("paystack_webhook_not_ussd_order", reference=str(data.get("reference") or ""))
        return jsonify({"success": True, "ignored": True, "reason": "not_ussd_order"}), 200
    if not result.get("success"):
        jlog("paystack_webhook_complete_failed", reference=str(data.get("reference") or ""), result=result)
        return jsonify(result), 400
    jlog("paystack_webhook_complete_ok", reference=str(data.get("reference") or ""), result=result)
    return jsonify(result), 200
