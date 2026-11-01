from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from bson import ObjectId
from flask import Blueprint, Response, jsonify, request

from db import db
from deposit import PAYSTACK_SECRET_KEY as DEPOSIT_PAYSTACK_SECRET_KEY
from .store_page import (
    _apply_store_pricing_to_service,
    _build_pricing_map,
    _extract_store_whatsapp,
    _load_services_for_store_view,
    generate_order_id,
    place_store_order,
    stores_col,
)

arkesel_ussd_bp = Blueprint("arkesel_ussd", __name__)

agent_codes_col = db["agent_codes"]
legacy_agent_code_col = db["agent_code"]
ussd_sessions_col = db["ussd_sessions"]
ussd_logs_col = db["ussd_request_logs"]
ussd_pending_payments_col = db["ussd_pending_payments"]
ussd_recent_agents_col = db["ussd_recent_agents"]
users_col = db["users"]

try:
    ussd_recent_agents_col.create_index("msisdn", unique=True)
except Exception as exc:
    print(f"[arkesel_ussd] recent-agent index setup failed: {exc}")

PAYSTACK_CHARGE_URL = "https://api.paystack.co/charge"
PAYSTACK_SUBMIT_OTP_URL = "https://api.paystack.co/charge/submit_otp"
PAYSTACK_CUSTOMER_URL = "https://api.paystack.co/customer"
PAYSTACK_FEE_RATE = 0.02
USSD_OTP_RESUME_MINUTES = max(int(os.getenv("USSD_OTP_RESUME_MINUTES", "10")), 1)
USSD_OTP_MAX_ATTEMPTS = max(int(os.getenv("USSD_OTP_MAX_ATTEMPTS", "3")), 1)
PAYSTACK_SECRET_KEY = (
    os.getenv("PAYSTACK_SECRET_KEY")
    or os.getenv("PAYSTACK_SK")
    or DEPOSIT_PAYSTACK_SECRET_KEY
    or ""
).strip()


def _plain(text: str) -> Response:
    return Response(text, mimetype="text/plain")


def _json_reply(session_id: str, user_id: str, msisdn: str, message: str, keep_open: bool):
    return jsonify(
        {
            "sessionID": session_id,
            "userID": user_id,
            "msisdn": msisdn,
            "message": message,
            "continueSession": bool(keep_open),
        }
    )


def _json_body(session_id: str, user_id: str, msisdn: str, message: str, keep_open: bool) -> Dict[str, Any]:
    return {
        "sessionID": session_id,
        "userID": user_id,
        "msisdn": msisdn,
        "message": message,
        "continueSession": bool(keep_open),
    }


def _log_ussd_request(data: Dict[str, Any], response_body: Optional[Dict[str, Any]] = None, error: Optional[str] = None) -> None:
    now = datetime.utcnow()
    session_id = _first(data, "sessionID", "sessionId", "session_id")
    user_id = _first(data, "userID", "user_id")
    msisdn = _first(data, "msisdn", "phoneNumber", "phone_number")
    try:
        ussd_logs_col.insert_one(
            {
                "provider": "arkesel",
                "session_id": session_id,
                "user_id": user_id,
                "msisdn": msisdn,
                "network": _first(data, "network"),
                "user_data": _first(data, "userData", "user_data", "text"),
                "new_session": data.get("newSession"),
                "payload": data,
                "response": response_body,
                "error": error,
                "path": request.path,
                "method": request.method,
                "content_type": request.content_type,
                "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr),
                "user_agent": request.headers.get("User-Agent"),
                "created_at": now,
            }
        )
    except Exception as exc:
        print(f"[arkesel_ussd] log failed: {exc}")


def _payload() -> Dict[str, Any]:
    data = request.get_json(silent=True) or request.form or request.values or {}
    return dict(data)


def _first(data: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def _is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _normalize_msisdn(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if digits.startswith("233") and len(digits) >= 12:
        return "0" + digits[-9:]
    if len(digits) == 9:
        return "0" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    return digits or str(raw or "").strip()


def _safe_object_id(value: Any) -> Optional[ObjectId]:
    if isinstance(value, ObjectId):
        return value
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _lookup_user_any_status(user_id: Any) -> Dict[str, Any]:
    oid = _safe_object_id(user_id)
    if not oid:
        return {}
    try:
        user_doc = users_col.find_one(
            {"_id": oid},
            {
                "email": 1,
                "phone": 1,
                "whatsapp": 1,
                "username": 1,
                "first_name": 1,
                "last_name": 1,
                "name": 1,
                "full_name": 1,
                "status": 1,
            },
        )
        return user_doc or {}
    except Exception:
        return {}


def _split_name_parts(user_doc: Dict[str, Any]) -> Tuple[str, str]:
    first = str(user_doc.get("first_name") or "").strip()
    last = str(user_doc.get("last_name") or "").strip()
    if first or last:
        return first, last

    full = ""
    for key in ("full_name", "name", "username"):
        value = str(user_doc.get(key) or "").strip()
        if value:
            full = value
            break
    if not full:
        return "", ""

    parts = [part for part in full.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _display_name_from_parts(first_name: str, last_name: str, fallback: str = "") -> str:
    full = " ".join(part for part in [str(first_name or "").strip(), str(last_name or "").strip()] if part).strip()
    return full or str(fallback or "").strip()


def _agent_identity(user_id: Any) -> Dict[str, str]:
    user_doc = _lookup_user_any_status(user_id)
    first_name, last_name = _split_name_parts(user_doc)
    fallback = ""
    for key in ("full_name", "name", "username"):
        value = str(user_doc.get(key) or "").strip()
        if value:
            fallback = value
            break
    if not fallback and user_doc.get("email"):
        fallback = str(user_doc.get("email")).split("@", 1)[0]

    return {
        "user_id": str(user_doc.get("_id") or user_id or "").strip(),
        "email": str(user_doc.get("email") or "").strip(),
        "phone": _normalize_msisdn(str(user_doc.get("phone") or "")),
        "first_name": first_name,
        "last_name": last_name,
        "display_name": _display_name_from_parts(first_name, last_name, fallback=fallback),
        "username": str(user_doc.get("username") or "").strip(),
    }


def _paystack_momo_provider(phone: str, fallback: Optional[str] = None) -> Optional[str]:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("233") and len(digits) >= 12:
        normalized = "0" + digits[-9:]
    elif len(digits) == 9:
        normalized = "0" + digits
    else:
        normalized = digits
    prefix = normalized[:3]
    if prefix in {"024", "025", "053", "054", "055", "059"}:
        return "mtn"
    if prefix in {"020", "050"}:
        return "vod"
    if prefix in {"026", "027", "056", "057"}:
        return "atl"
    fb = (fallback or os.getenv("PAYSTACK_GH_MOMO_PROVIDER_FALLBACK") or "").strip().lower()
    return fb if fb in {"mtn", "vod", "atl"} else None


def _ussd_paystack_reference(order_id: str) -> str:
    return f"USSD-{order_id}-{uuid.uuid4().hex[:8]}".upper()


def _paystack_ussd_email(phone: str) -> str:
    normalized = _normalize_msisdn(phone)
    digits = "".join(ch for ch in normalized if ch.isdigit())
    return f"{digits or 'customer'}hansmart@gmail.com"


def _calc_paystack_totals(base_total: float) -> Dict[str, float]:
    base = round(float(base_total or 0.0), 2)
    fee = round(base * PAYSTACK_FEE_RATE, 2)
    total = round(base + fee, 2)
    return {"base_total": base, "fee": fee, "paystack_total": total}


def _otp_resume_message(display_text: str = "") -> str:
    instruction = str(display_text or "").strip()
    lines = []
    if instruction:
        lines.append(instruction)
    lines.extend(
        [
            "You may leave this session to retrieve or generate the OTP/voucher.",
            f"Dial this shortcode again within {USSD_OTP_RESUME_MINUTES} minutes to continue without entering your agent code.",
            "Enter OTP/voucher code:",
        ]
    )
    return "\n".join(lines)


def _pending_payment_message(pending_doc: Dict[str, Any]) -> str:
    status = str(pending_doc.get("status") or "").strip().lower()
    if status == "awaiting_paystack_otp":
        return _otp_resume_message(str(pending_doc.get("otp_display_text") or ""))
    if status == "order_create_failed":
        return "Payment received. We are finalizing your order automatically. Please check order status shortly."
    return "Payment approval is pending. Complete the MTN/mobile money approval. Your order will process automatically after confirmation."


def _find_resumable_payment(msisdn: str) -> Optional[Dict[str, Any]]:
    phone = _normalize_msisdn(msisdn)
    if not phone:
        return None

    now = datetime.utcnow()
    return ussd_pending_payments_col.find_one(
        {
            "payer_phone": phone,
            "payment_status": {"$in": [None, "pending", "paid"]},
            "status": {
                "$in": [
                    "awaiting_paystack_otp",
                    "awaiting_payment",
                    "payment_processing",
                    "creating_order",
                    "order_create_failed",
                ]
            },
            "$or": [
                {"status": {"$ne": "awaiting_paystack_otp"}},
                {"otp_expires_at": {"$gt": now}},
            ],
        },
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def _resume_pending_payment_session(
    session_id: str,
    user_id: str,
    msisdn: str,
    service_code: str,
    network: str,
) -> Optional[Dict[str, Any]]:
    pending_doc = _find_resumable_payment(msisdn)
    if not pending_doc:
        return None

    now = datetime.utcnow()
    status = str(pending_doc.get("status") or "").strip().lower()
    session_status = "awaiting_paystack_otp" if status == "awaiting_paystack_otp" else "awaiting_payment_confirmation"
    _upsert_session(
        session_id,
        {
            "user_id": user_id,
            "service_code": service_code or pending_doc.get("service_code"),
            "msisdn": msisdn,
            "network": network,
            "status": session_status,
            "order_id": pending_doc.get("order_id"),
            "payment_reference": pending_doc.get("payment_reference") or pending_doc.get("paystack_reference"),
            "resumed_payment_id": pending_doc.get("_id"),
            "resumed_at": now,
        },
    )
    ussd_pending_payments_col.update_one(
        {"_id": pending_doc["_id"]},
        {
            "$set": {"latest_session_id": session_id, "last_resumed_at": now, "updated_at": now},
            "$inc": {"resume_count": 1},
        },
    )
    return {"pending": pending_doc, "keep_open": status == "awaiting_paystack_otp", "message": _pending_payment_message(pending_doc)}


def _paystack_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def _ensure_paystack_customer(identity: Dict[str, str], payer_phone: str, agent_code: str) -> Dict[str, Any]:
    email = str(identity.get("email") or "").strip()
    if not PAYSTACK_SECRET_KEY or not email:
        return {}

    first_name = str(identity.get("first_name") or "").strip()
    last_name = str(identity.get("last_name") or "").strip()
    phone = _normalize_msisdn(identity.get("phone") or payer_phone)
    metadata = {
        "source": "arkesel_ussd",
        "agent_code": str(agent_code or "").strip(),
        "agent_user_id": str(identity.get("user_id") or "").strip(),
        "agent_display_name": str(identity.get("display_name") or "").strip(),
        "agent_username": str(identity.get("username") or "").strip(),
    }

    try:
        fetch_resp = requests.get(
            f"{PAYSTACK_CUSTOMER_URL}/{quote(email, safe='')}",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            timeout=20,
        )
        fetch_data = fetch_resp.json() if fetch_resp.content else {}
    except Exception as exc:
        print(f"[arkesel_ussd] paystack customer fetch failed: {exc}")
        return {}

    if fetch_resp.status_code < 400 and fetch_data.get("status"):
        customer = fetch_data.get("data") if isinstance(fetch_data.get("data"), dict) else {}
        customer_code = str(customer.get("customer_code") or "").strip()
        if customer_code and (first_name or last_name or phone):
            update_payload = {"metadata": metadata}
            if first_name:
                update_payload["first_name"] = first_name
            if last_name:
                update_payload["last_name"] = last_name
            if phone:
                update_payload["phone"] = phone
            try:
                requests.put(
                    f"{PAYSTACK_CUSTOMER_URL}/{quote(customer_code, safe='')}",
                    json=update_payload,
                    headers=_paystack_headers(),
                    timeout=20,
                )
            except Exception as exc:
                print(f"[arkesel_ussd] paystack customer update failed: {exc}")
        return customer

    create_payload = {"email": email, "metadata": metadata}
    if first_name:
        create_payload["first_name"] = first_name
    if last_name:
        create_payload["last_name"] = last_name
    if phone:
        create_payload["phone"] = phone

    try:
        create_resp = requests.post(
            PAYSTACK_CUSTOMER_URL,
            json=create_payload,
            headers=_paystack_headers(),
            timeout=20,
        )
        create_data = create_resp.json() if create_resp.content else {}
        if create_resp.status_code < 400 and create_data.get("status"):
            created = create_data.get("data") if isinstance(create_data.get("data"), dict) else {}
            return created
        print(f"[arkesel_ussd] paystack customer create failed: {create_data.get('message') or create_resp.status_code}")
    except Exception as exc:
        print(f"[arkesel_ussd] paystack customer create error: {exc}")
    return {}


def _start_paystack_momo_charge(
    *,
    order_id: str,
    amount_ghs: float,
    payer_phone: str,
    agent_code: str,
    agent_identity: Optional[Dict[str, str]],
    session_id: str,
    reference: str,
) -> Tuple[bool, Dict[str, Any], str]:
    if not PAYSTACK_SECRET_KEY:
        return False, {}, "Paystack secret key is not configured."

    provider = _paystack_momo_provider(payer_phone)
    if not provider:
        return False, {}, "Unsupported mobile money network."

    normalized_phone = _normalize_msisdn(payer_phone)
    amount_pesewas = int(round(float(amount_ghs or 0) * 100))
    if amount_pesewas <= 0:
        return False, {}, "Invalid payment amount."

    resolved_identity = agent_identity or {}
    customer_email = str(resolved_identity.get("email") or "").strip() or _paystack_ussd_email(normalized_phone)
    customer_first_name = str(resolved_identity.get("first_name") or "").strip()
    customer_last_name = str(resolved_identity.get("last_name") or "").strip()
    customer_display_name = str(resolved_identity.get("display_name") or "").strip()
    paystack_customer = _ensure_paystack_customer(resolved_identity, normalized_phone, agent_code)

    payload = {
        "email": customer_email,
        "amount": amount_pesewas,
        "currency": "GHS",
        "reference": reference,
        "mobile_money": {
            "phone": normalized_phone,
            "provider": provider,
        },
        "metadata": {
            "order_id": order_id,
            "channel": "arkesel_ussd",
            "agent_code": agent_code,
            "agent_user_id": str(resolved_identity.get("user_id") or "").strip(),
            "agent_first_name": customer_first_name,
            "agent_last_name": customer_last_name,
            "agent_display_name": customer_display_name,
            "agent_email": customer_email,
            "session_id": session_id,
        },
    }
    if paystack_customer:
        payload["metadata"]["paystack_customer_code"] = str(paystack_customer.get("customer_code") or "").strip()
    try:
        resp = requests.post(PAYSTACK_CHARGE_URL, json=payload, headers=_paystack_headers(), timeout=20)
        data = resp.json() if resp.content else {}
    except Exception as exc:
        return False, {}, f"Paystack charge error: {exc}"

    if resp.status_code >= 400 or not data.get("status"):
        return False, data, data.get("message") or "Paystack charge failed."
    return True, data, ""


def _submit_paystack_otp(otp: str, reference: str) -> Tuple[bool, Dict[str, Any], str]:
    if not PAYSTACK_SECRET_KEY:
        return False, {}, "Paystack secret key is not configured."
    payload = {"otp": str(otp or "").strip(), "reference": str(reference or "").strip()}
    if not payload["otp"] or not payload["reference"]:
        return False, {}, "OTP and payment reference are required."
    try:
        resp = requests.post(PAYSTACK_SUBMIT_OTP_URL, json=payload, headers=_paystack_headers(), timeout=20)
        data = resp.json() if resp.content else {}
    except Exception as exc:
        return False, {}, f"Paystack OTP error: {exc}"
    if resp.status_code >= 400 or not data.get("status"):
        return False, data, data.get("message") or "Paystack OTP failed."
    return True, data, ""


def _paystack_data(charge_response: Dict[str, Any]) -> Dict[str, Any]:
    data = charge_response.get("data") if isinstance(charge_response.get("data"), dict) else {}
    return data if isinstance(data, dict) else {}


def _update_order_payment_reference(order_id: str, reference: str, charge_response: Dict[str, Any]) -> None:
    data = _paystack_data(charge_response)
    db["orders"].update_one(
        {"order_id": order_id},
        {
            "$set": {
                "payment_reference": reference,
                "paystack_reference": reference,
                "payment_access_code": data.get("access_code"),
                "payment_charge_response": charge_response,
                "updated_at": datetime.utcnow(),
            }
        },
    )
    db["transactions"].update_one(
        {"reference": order_id, "source": "arkesel_ussd"},
        {
            "$set": {
                "payment_reference": reference,
                "payment_provider": "paystack",
                "payment_channel": "mobile_money",
                "updated_at": datetime.utcnow(),
            }
        },
    )


def _save_pending_payment(doc: Dict[str, Any]) -> None:
    now = datetime.utcnow()
    doc = {**doc, "updated_at": now}
    created_at = doc.pop("created_at", now)
    ussd_pending_payments_col.update_one(
        {"payment_reference": doc.get("payment_reference")},
        {
            "$set": doc,
            "$setOnInsert": {"created_at": created_at},
        },
        upsert=True,
    )


def _complete_paid_order(order_id: str, data: Dict[str, Any]) -> bool:
    try:
        from paystack_webhook import complete_arkesel_ussd_payment_by_reference

        reference = str(data.get("reference") or "").strip()
        result = complete_arkesel_ussd_payment_by_reference(reference, data)
        return bool(result.get("success"))
    except Exception as exc:
        print(f"[arkesel_ussd] immediate payment completion failed: {exc}")
        return False


def _spawn_pending_payment_recovery(reference: str, session_id: str) -> None:
    reference = str(reference or "").strip()
    if not reference:
        return

    def _runner():
        try:
            from paystack_webhook import reconcile_arkesel_pending_payments

            for attempt in range(1, 5):
                time.sleep(15)
                result = reconcile_arkesel_pending_payments(reference=reference, limit=1)
                if int(result.get("completed") or 0) > 0:
                    _upsert_session(
                        session_id,
                        {
                            "status": "completed",
                            "payment_reference": reference,
                            "recovered_after_attempt": attempt,
                        },
                    )
                    return
                if int(result.get("marked_failed") or 0) > 0:
                    _upsert_session(
                        session_id,
                        {
                            "status": "payment_failed",
                            "payment_reference": reference,
                            "recovered_after_attempt": attempt,
                        },
                    )
                    return

            _upsert_session(
                session_id,
                {
                    "status": "awaiting_payment_confirmation",
                    "payment_reference": reference,
                },
            )
        except Exception as exc:
            print(f"[arkesel_ussd] pending payment recovery failed: {exc}")
            _upsert_session(
                session_id,
                {
                    "status": "awaiting_payment_confirmation",
                    "payment_reference": reference,
                    "recovery_error": str(exc),
                },
            )

    try:
        threading.Thread(target=_runner, daemon=True).start()
    except Exception as exc:
        print(f"[arkesel_ussd] pending payment recovery spawn failed: {exc}")


def _handle_paystack_charge_status(order_id: str, session_id: str, charge_response: Dict[str, Any]) -> Dict[str, Any]:
    data = _paystack_data(charge_response)
    payment_status = str(data.get("status") or "").strip().lower()
    reference = str(data.get("reference") or "").strip()
    if reference:
        ussd_pending_payments_col.update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "payment_reference": reference,
                    "paystack_reference": reference,
                    "payment_access_code": data.get("access_code"),
                    "payment_charge_response": charge_response,
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    if payment_status == "send_otp":
        now = datetime.utcnow()
        display_text = str(data.get("display_text") or charge_response.get("message") or "").strip()
        otp_expires_at = now + timedelta(minutes=USSD_OTP_RESUME_MINUTES)
        ussd_pending_payments_col.update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "status": "awaiting_paystack_otp",
                    "payment_status": "pending",
                    "otp_display_text": display_text,
                    "otp_expires_at": otp_expires_at,
                    "otp_attempts": 0,
                    "latest_session_id": session_id,
                    "updated_at": now,
                }
            },
        )
        _upsert_session(
            session_id,
            {
                "status": "awaiting_paystack_otp",
                "order_id": order_id,
                "payment_reference": reference,
                "otp_display_text": display_text,
                "otp_expires_at": otp_expires_at,
            },
        )
        return {
            "success": True,
            "status": "awaiting_paystack_otp",
            "keep_open": True,
            "session_status": "awaiting_paystack_otp",
            "message": _otp_resume_message(display_text),
        }

    if payment_status == "pending":
        now = datetime.utcnow()
        db["orders"].update_one(
            {"order_id": order_id},
            {"$set": {"payment_prompt_sent_at": now, "status": "awaiting_payment", "updated_at": now}},
        )
        ussd_pending_payments_col.update_one(
            {"order_id": order_id},
            {"$set": {"status": "awaiting_payment", "payment_prompt_sent_at": now, "updated_at": now}},
        )
        _spawn_pending_payment_recovery(reference, session_id)
        return {
            "success": True,
            "status": "awaiting_payment",
            "keep_open": False,
            "session_status": "awaiting_payment_confirmation",
            "message": "Payment prompt sent. If not received, dial *170# and confirm or Go to Approvals. Thank you.",
        }

    if payment_status == "pay_offline":
        now = datetime.utcnow()
        db["orders"].update_one(
            {"order_id": order_id},
            {"$set": {"payment_prompt_sent_at": now, "status": "awaiting_payment", "updated_at": now}},
        )
        ussd_pending_payments_col.update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "status": "awaiting_payment",
                    "payment_status": "pending",
                    "payment_prompt_sent_at": now,
                    "updated_at": now,
                }
            },
        )
        _spawn_pending_payment_recovery(reference, session_id)
        return {
            "success": True,
            "status": "awaiting_payment",
            "keep_open": False,
            "session_status": "awaiting_payment_confirmation",
            "message": "Complete the mobile money approval on your phone. We will record the order once Paystack confirms the payment.",
        }

    if payment_status == "success":
        if _complete_paid_order(order_id, data):
            return {
                "success": True,
                "status": "processing",
                "keep_open": False,
                "session_status": "completed",
                "message": "Payment received. Your SirHans order is processing.",
            }
        now = datetime.utcnow()
        ussd_pending_payments_col.update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "status": "order_create_failed",
                    "payment_status": "paid",
                    "payment_charge_response": charge_response,
                    "updated_at": now,
                }
            },
        )
        _spawn_pending_payment_recovery(reference, session_id)
        return {
            "success": True,
            "status": "awaiting_payment",
            "keep_open": False,
            "session_status": "awaiting_payment_confirmation",
            "message": "Payment received. Finalizing your order now.",
        }

    if payment_status in {"timeout", "failed"}:
        _mark_payment_start_failed(order_id, charge_response, f"Paystack charge {payment_status}.")
        return {"success": False, "message": "Payment failed."}

    return {
        "success": True,
        "status": "awaiting_payment",
        "keep_open": False,
        "session_status": "awaiting_payment_confirmation",
        "message": "Payment is processing. We will complete your order once payment is confirmed.",
    }


def _handle_paystack_otp_input(session_id: str, user_input: str) -> Dict[str, Any]:
    session_doc = ussd_sessions_col.find_one({"session_id": session_id}) or {}
    reference = str(session_doc.get("payment_reference") or "").strip()
    order_id = str(session_doc.get("order_id") or "").strip()
    otp = str(user_input or "").strip()
    if not otp or not otp.isdigit() or not 4 <= len(otp) <= 10:
        return {"success": False, "keep_open": True, "message": "Enter a valid numeric OTP/voucher code."}

    now = datetime.utcnow()
    pending_doc = ussd_pending_payments_col.find_one(
        {
            "$or": [{"payment_reference": reference}, {"paystack_reference": reference}, {"order_id": order_id}],
            "status": "awaiting_paystack_otp",
        }
    )
    if not pending_doc:
        return {"success": False, "keep_open": False, "message": "This OTP request is no longer active. Check your order status."}
    if pending_doc.get("otp_expires_at") and pending_doc["otp_expires_at"] <= now:
        ussd_pending_payments_col.update_one(
            {"_id": pending_doc["_id"]},
            {"$set": {"status": "otp_expired", "updated_at": now}},
        )
        _upsert_session(session_id, {"status": "otp_expired"})
        return {"success": False, "keep_open": False, "message": "OTP session expired. Please start the order again."}

    claim = ussd_pending_payments_col.update_one(
        {
            "_id": pending_doc["_id"],
            "status": "awaiting_paystack_otp",
            "$or": [
                {"otp_submission_in_progress": {"$ne": True}},
                {"otp_submission_started_at": {"$lt": now - timedelta(minutes=1)}},
            ],
        },
        {
            "$set": {
                "otp_submission_in_progress": True,
                "otp_submission_started_at": now,
                "latest_session_id": session_id,
                "updated_at": now,
            }
        },
    )
    if not claim.modified_count:
        return {"success": False, "keep_open": True, "message": "OTP submission is already processing. Please wait and try again."}

    try:
        ok, response, reason = _submit_paystack_otp(otp, reference)
    except Exception as exc:
        ussd_pending_payments_col.update_one(
            {"_id": pending_doc["_id"]},
            {
                "$set": {"otp_last_error": str(exc), "updated_at": datetime.utcnow()},
                "$unset": {"otp_submission_in_progress": "", "otp_submission_started_at": ""},
            },
        )
        return {"success": False, "keep_open": True, "message": "Could not submit OTP right now. Please try again."}

    if not ok:
        attempts = int(pending_doc.get("otp_attempts") or 0) + 1
        terminal = attempts >= USSD_OTP_MAX_ATTEMPTS
        ussd_pending_payments_col.update_one(
            {"_id": pending_doc["_id"]},
            {
                "$set": {
                    "status": "payment_failed" if terminal else "awaiting_paystack_otp",
                    "otp_attempts": attempts,
                    "otp_last_error": reason,
                    "otp_last_response": response,
                    "updated_at": datetime.utcnow(),
                },
                "$unset": {"otp_submission_in_progress": "", "otp_submission_started_at": ""},
            },
        )
        if terminal:
            if order_id:
                _mark_payment_start_failed(order_id, response, reason)
            _upsert_session(session_id, {"status": "payment_failed", "failure": reason})
            return {"success": False, "keep_open": False, "message": "Maximum OTP attempts reached. Please start the order again."}
        remaining = USSD_OTP_MAX_ATTEMPTS - attempts
        return {
            "success": False,
            "keep_open": True,
            "message": f"OTP/voucher was not accepted. Try again ({remaining} attempt{'s' if remaining != 1 else ''} left).",
        }

    data = _paystack_data(response)
    status = str(data.get("status") or "").strip().lower()
    if status == "success" and order_id:
        if _complete_paid_order(order_id, data):
            _upsert_session(session_id, {"status": "completed", "otp_response": response})
            return {"success": True, "keep_open": False, "message": "Payment received. Your SirHans order is processing."}
        now = datetime.utcnow()
        ussd_pending_payments_col.update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "status": "order_create_failed",
                    "payment_status": "paid",
                    "otp_response": response,
                    "updated_at": now,
                }
            },
        )
        _upsert_session(session_id, {"status": "awaiting_payment_confirmation", "otp_response": response})
        _spawn_pending_payment_recovery(reference, session_id)
        return {
            "success": True,
            "keep_open": False,
            "message": "Payment received. Finalizing your order now.",
        }

    if status in {"pending", "pay_offline"}:
        ussd_pending_payments_col.update_one(
            {"_id": pending_doc["_id"]},
            {
                "$set": {"status": "awaiting_payment", "otp_response": response, "updated_at": datetime.utcnow()},
                "$unset": {"otp_submission_in_progress": "", "otp_submission_started_at": ""},
            },
        )
        _upsert_session(session_id, {"status": "awaiting_payment_confirmation", "otp_response": response})
        _spawn_pending_payment_recovery(reference, session_id)
        return {"success": True, "keep_open": False, "message": "Payment is processing. We will complete your order once payment is confirmed."}

    if status == "send_otp":
        display_text = str(data.get("display_text") or pending_doc.get("otp_display_text") or "").strip()
        ussd_pending_payments_col.update_one(
            {"_id": pending_doc["_id"]},
            {
                "$set": {
                    "status": "awaiting_paystack_otp",
                    "otp_display_text": display_text,
                    "otp_response": response,
                    "updated_at": datetime.utcnow(),
                },
                "$inc": {"otp_attempts": 1},
                "$unset": {"otp_submission_in_progress": "", "otp_submission_started_at": ""},
            },
        )
        return {"success": False, "keep_open": True, "message": _otp_resume_message(display_text)}

    if order_id:
        _mark_payment_start_failed(order_id, response, f"Paystack OTP status {status or 'failed'}.")
    _upsert_session(session_id, {"status": "payment_failed", "otp_response": response})
    return {"success": False, "keep_open": False, "message": "Payment failed."}


def _mark_payment_start_failed(order_id: str, charge_response: Dict[str, Any], reason: str) -> None:
    try:
        db["ussd_pending_payments"].update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "status": "payment_failed",
                    "payment_status": "failed",
                    "payment_start_error": reason,
                    "payment_charge_response": charge_response,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        db["orders"].update_one(
            {"order_id": order_id},
            {
                "$set": {
                    "status": "payment_failed",
                    "payment_status": "failed",
                    "payment_start_error": reason,
                    "payment_charge_response": charge_response,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        db["transactions"].update_one(
            {"reference": order_id, "source": "arkesel_ussd"},
            {
                "$set": {
                    "status": "failed",
                    "payment_status": "failed",
                    "payment_start_error": reason,
                    "updated_at": datetime.utcnow(),
                }
            },
        )
    except Exception as exc:
        print(f"[arkesel_ussd] payment failure update failed: {exc}")


def _find_agent_code(code: str) -> Optional[Dict[str, Any]]:
    query = {"agent_code": code}
    doc = agent_codes_col.find_one(query)
    if not doc:
        doc = legacy_agent_code_col.find_one(query)
    if not doc:
        return None
    if str(doc.get("status") or "").strip().lower() != "active":
        return None
    return doc


def _is_public_agent_code(agent_code_doc: Optional[Dict[str, Any]]) -> bool:
    return str((agent_code_doc or {}).get("type") or "").strip().lower() == "public"


def _public_store_doc() -> Dict[str, Any]:
    return {"name": "Sir Hans", "slug": "sir-hans", "public_index": True}


def _load_public_services() -> List[Dict[str, Any]]:
    try:
        from index import load_services_for_landing

        services, _products = load_services_for_landing()
        return [svc for svc in services if svc.get("can_order") and svc.get("offers")]
    except Exception as exc:
        print(f"[arkesel_ussd] public services load failed: {exc}")
        return []


def _find_agent_store(agent_user_id: ObjectId) -> Optional[Dict[str, Any]]:
    return stores_col.find_one(
        {"owner_id": agent_user_id, "status": {"$ne": "deleted"}},
        sort=[("updated_at", -1), ("created_at", -1)],
    )


def _session_agent_identity(session_doc: Dict[str, Any]) -> Dict[str, str]:
    identity = session_doc.get("agent_identity")
    if isinstance(identity, dict) and any(str(v or "").strip() for v in identity.values()):
        return {str(k): str(v or "").strip() for k, v in identity.items()}
    return _agent_identity(session_doc.get("agent_user_id"))


def _store_contact_info(store_doc: Dict[str, Any], agent_user_id: Any) -> Dict[str, str]:
    wa = _extract_store_whatsapp(store_doc or {})
    agent_doc = _lookup_user_any_status(agent_user_id)
    store_name = str((store_doc or {}).get("name") or "Store").strip() or "Store"
    whatsapp = str(wa.get("number_raw") or agent_doc.get("whatsapp") or "").strip()
    phone = _normalize_msisdn(str(agent_doc.get("phone") or ""))
    return {
        "store_name": store_name,
        "whatsapp": whatsapp,
        "phone": phone,
    }


def _contact_message(store_doc: Dict[str, Any], agent_user_id: Any) -> str:
    info = _store_contact_info(store_doc, agent_user_id)
    rows = [f"Store: {info['store_name']}"]
    if info["whatsapp"]:
        rows.append(f"WhatsApp: {info['whatsapp']}")
    if info["phone"]:
        rows.append(f"Phone: {info['phone']}")
    if len(rows) == 1:
        rows.append("No contact number available.")
    return "Contact Us\n" + "\n".join(rows)


def _fmt_ussd_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    try:
        return str(dt or "").strip()
    except Exception:
        return ""


def _latest_order_status_message(phone: str, session_doc: Dict[str, Any], store_doc: Dict[str, Any]) -> str:
    normalized_phone = _normalize_msisdn(phone)
    if not normalized_phone:
        return "Invalid phone number"

    query: Dict[str, Any] = {"items.phone": normalized_phone}
    if session_doc.get("flow") == "public_index":
        query["$or"] = [
            {"channel": "arkesel_ussd"},
            {"paid_from": "public_paystack"},
            {"source.target": "public_index"},
        ]
    else:
        slug = str((store_doc or {}).get("slug") or session_doc.get("store_slug") or "").strip()
        if slug:
            query["store_slug"] = slug

    order_doc = db["orders"].find_one(
        query,
        {
            "order_id": 1,
            "status": 1,
            "created_at": 1,
            "updated_at": 1,
            "items": 1,
        },
        sort=[("created_at", -1)],
    )
    if not order_doc:
        return "No order found for this number"

    matched_item = None
    for item in order_doc.get("items") or []:
        if _normalize_msisdn(item.get("phone") or "") == normalized_phone:
            matched_item = item
            break

    rows = [
        "Latest Order Status",
        f"Order ID: {order_doc.get('order_id') or '-'}",
        f"Status: {str(order_doc.get('status') or 'Pending').capitalize()}",
    ]
    if matched_item:
        rows.append(f"Service: {matched_item.get('serviceName') or '-'}")
        if matched_item.get("value"):
            rows.append(f"Offer: {matched_item.get('value')}")
    when = _fmt_ussd_dt(order_doc.get("updated_at") or order_doc.get("created_at"))
    if when:
        rows.append(f"Updated: {when}")
    return "\n".join(rows)


def _session_store(session_doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    store_id = session_doc.get("store_id")
    if store_id:
        try:
            oid = store_id if isinstance(store_id, ObjectId) else ObjectId(str(store_id))
            store_doc = stores_col.find_one({"_id": oid, "status": {"$ne": "deleted"}})
            if store_doc:
                return store_doc
        except Exception:
            pass

    agent_user_id = session_doc.get("agent_user_id")
    if agent_user_id:
        try:
            oid = agent_user_id if isinstance(agent_user_id, ObjectId) else ObjectId(str(agent_user_id))
            return _find_agent_store(oid)
        except Exception:
            pass
    return None


def _load_store_services(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    scope = store_doc.get("service_scope") or "all"
    service_ids = store_doc.get("service_ids") or []
    services = _load_services_for_store_view(scope, service_ids)
    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    priced = [_apply_store_pricing_to_service(s, percent_default, per_map) for s in services]
    return [s for s in priced if s.get("can_order") and s.get("offers")]


def _find_recent_agent(msisdn: str) -> Optional[Dict[str, Any]]:
    phone = _normalize_msisdn(msisdn)
    if not phone:
        return None
    return ussd_recent_agents_col.find_one({"msisdn": phone})


def _forget_recent_agent(msisdn: str) -> None:
    phone = _normalize_msisdn(msisdn)
    if phone:
        ussd_recent_agents_col.delete_one({"msisdn": phone})


def _remember_recent_agent(msisdn: str, context: Dict[str, Any]) -> None:
    phone = _normalize_msisdn(msisdn)
    if not phone:
        return
    now = datetime.utcnow()
    ussd_recent_agents_col.update_one(
        {"msisdn": phone},
        {
            "$set": {
                "agent_code": context.get("agent_code"),
                "agent_user_id": context.get("agent_user_id"),
                "agent_code_type": "public" if context.get("is_public") else "store",
                "store_id": (context.get("store_doc") or {}).get("_id"),
                "store_slug": (context.get("store_doc") or {}).get("slug"),
                "last_used_at": now,
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def _resolve_agent_context(agent_code: str) -> Tuple[Optional[Dict[str, Any]], str]:
    code = str(agent_code or "").strip()
    agent_code_doc = _find_agent_code(code)
    if not agent_code_doc:
        return None, "Invalid agent code"

    is_public = _is_public_agent_code(agent_code_doc)
    agent_user_id = agent_code_doc.get("user_id")
    agent_identity = _agent_identity(agent_user_id)
    if is_public:
        store_doc = _public_store_doc()
        services = _load_public_services()
    else:
        if not agent_user_id:
            return None, "Invalid agent code"
        store_doc = _find_agent_store(agent_user_id)
        if not store_doc:
            return None, "Agent store not found"
        services = _load_store_services(store_doc)

    if not services:
        return None, "No services available"
    return {
        "agent_code": code,
        "agent_code_doc": agent_code_doc,
        "agent_user_id": agent_user_id,
        "agent_identity": agent_identity,
        "is_public": is_public,
        "store_doc": store_doc,
        "services": services,
    }, ""


def _activate_agent_session(session_id: str, msisdn: str, agent_code: str) -> Tuple[Optional[Dict[str, Any]], str]:
    context, error = _resolve_agent_context(agent_code)
    if not context:
        return None, error

    store_doc = context["store_doc"]
    is_public = bool(context["is_public"])
    _upsert_session(
        session_id,
        {
            "agent_code": context["agent_code"],
            "agent_user_id": context["agent_user_id"],
            "agent_identity": context["agent_identity"],
            "agent_code_type": "public" if is_public else "store",
            "flow": "public_index" if is_public else "store",
            "store_id": store_doc.get("_id"),
            "store_slug": store_doc.get("slug"),
            "store_name": store_doc.get("name"),
            "recent_agent_code": None,
            "status": "selecting_service",
        },
    )
    _remember_recent_agent(msisdn, context)
    return context, ""


def _menu(title: str, items: List[str], prefix: str = "CON") -> str:
    body = "\n".join(items)
    return f"{prefix} {title}\n{body}" if body else f"{prefix} {title}"


def _upsert_session(session_id: str, updates: Dict[str, Any]) -> None:
    now = datetime.utcnow()
    ussd_sessions_col.update_one(
        {"session_id": session_id},
        {
            "$set": {**updates, "updated_at": now},
            "$setOnInsert": {"session_id": session_id, "created_at": now},
        },
        upsert=True,
    )


def _choice_index(value: str, total: int) -> Optional[int]:
    try:
        idx = int(value) - 1
    except Exception:
        return None
    if idx < 0 or idx >= total:
        return None
    return idx


def _service_menu(services: List[Dict[str, Any]], store_name: str = "") -> str:
    rows = [f"{i}. {svc.get('name')}" for i, svc in enumerate(services[:8], start=1)]
    rows.append(f"{len(rows) + 1}. Contact Us")
    rows.append(f"{len(rows) + 1}. Check Order Status")
    title = f"Welcome {store_name}\nSelect Service" if store_name else "Select Service"
    return title + "\n" + "\n".join(rows)


def _service_choice(value: str, services: List[Dict[str, Any]]) -> Optional[str | int]:
    visible_count = min(len(services), 8)
    try:
        idx = int(value)
    except Exception:
        return None
    if 1 <= idx <= visible_count:
        return idx - 1
    if idx == visible_count + 1:
        return "contact"
    if idx == visible_count + 2:
        return "check_status"
    return None


def _offer_menu(offers: List[Dict[str, Any]]) -> str:
    rows = []
    for i, offer in enumerate(offers[:8], start=1):
        label = offer.get("value_text") or "-"
        amount = float(offer.get("total") or 0)
        rows.append(f"{i}. {label} - GHS {amount:g}")
    return "Select Offer\n" + "\n".join(rows)


def _offer_page_menu(offers: List[Dict[str, Any]], page: int = 0, page_size: int = 5) -> str:
    total = len(offers or [])
    page = max(int(page or 0), 0)
    start = page * page_size
    chunk = (offers or [])[start : start + page_size]
    rows: List[str] = []
    for i, offer in enumerate(chunk, start=1):
        label = offer.get("value_text") or "-"
        amount = float(offer.get("total") or 0)
        rows.append(f"{i}. {label} - GHS {amount:g}")
    nav_index = len(chunk)
    if start + page_size < total:
        nav_index += 1
        rows.append(f"{nav_index}. More offers")
    if page > 0:
        nav_index += 1
        rows.append(f"{nav_index}. Back")
    return "Select Offer\n" + "\n".join(rows)


def _offer_choice(value: str, offers: List[Dict[str, Any]], page: int = 0, page_size: int = 5) -> Optional[str | int]:
    page = max(int(page or 0), 0)
    start = page * page_size
    chunk = (offers or [])[start : start + page_size]
    try:
        idx = int(value)
    except Exception:
        return None
    if 1 <= idx <= len(chunk):
        return start + idx - 1
    next_index = len(chunk) + 1
    if start + page_size < len(offers or []):
        if idx == next_index:
            return "next_page"
        next_index += 1
    if page > 0 and idx == next_index:
        return "prev_page"
    return None


def _selected_context(session_doc: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    if session_doc.get("flow") == "public_index":
        store_doc = _public_store_doc()
        services = _load_public_services()
    else:
        store_doc = _session_store(session_doc)
        if not store_doc:
            return None, None, None, []
        services = _load_store_services(store_doc)

    selected_service_id = str(session_doc.get("selected_service_id") or "")
    selected_service = None
    for svc in services:
        if str(svc.get("_id")) == selected_service_id:
            selected_service = svc
            break

    offers = (selected_service or {}).get("offers") or []
    selected_offer = None
    try:
        offer_idx = int(session_doc.get("selected_offer_index"))
        if 0 <= offer_idx < len(offers):
            selected_offer = offers[offer_idx]
    except Exception:
        pass

    return store_doc, selected_service, selected_offer, services


def _confirm_message(service_name: str, label: str, amount: float, recipient_phone: str) -> str:
    return (
        "Confirm Order\n"
        f"{service_name}\n"
        f"{label} GHS {amount:g}\n"
        f"To: {recipient_phone}\n"
        "1. Place Order\n"
        "2. Cancel"
    )


def _ussd_known_number_ineligible(selected_service: Dict[str, Any], recipient_phone: str) -> bool:
    try:
        from checkout import _known_number_exists, _known_number_local, _service_requires_known_number_verification  # type: ignore
    except Exception:
        from ..checkout import _known_number_exists, _known_number_local, _service_requires_known_number_verification  # type: ignore

    line = {
        "serviceId": str(selected_service.get("_id") or ""),
        "serviceName": selected_service.get("name"),
        "service_network": selected_service.get("service_network"),
        "network": selected_service.get("network"),
        "phone": recipient_phone,
    }
    if not _service_requires_known_number_verification(line, selected_service):
        return False
    normalized_phone = _known_number_local(recipient_phone)
    if not normalized_phone:
        return True
    return not _known_number_exists(normalized_phone)


def _place_ussd_order(
    session_doc: Dict[str, Any],
    store_doc: Dict[str, Any],
    selected_service: Dict[str, Any],
    selected_offer: Dict[str, Any],
    recipient_phone: str,
    payer_phone: str,
    session_id: str,
    service_code: str,
):
    agent_identity = _session_agent_identity(session_doc)
    amount = float(selected_offer.get("total") or 0)
    paystack_totals = _calc_paystack_totals(amount)
    label = selected_offer.get("value_text") or "-"
    service_name = selected_service.get("name") or "Service"
    cart_item = {
        "serviceId": str(selected_service.get("_id")),
        "serviceName": service_name,
        "service_network": selected_service.get("service_network"),
        "network": selected_service.get("network"),
        "phone": recipient_phone,
        "value": label,
        "value_obj": selected_offer.get("value"),
        "base_amount": float(selected_offer.get("amount") or 0),
        "amount": amount,
    }
    order_id = generate_order_id()
    payment_reference = _ussd_paystack_reference(order_id)
    place_payload = {
        "order_id": order_id,
        "store_doc": store_doc,
        "slug": store_doc.get("slug"),
        "cart": [cart_item],
        "payment_status": "paid",
        "paid_from": "paystack_mobile_money",
        "order_status": "pending",
        "defer_provider_processing": False,
        "payment_provider": "paystack",
        "payment_channel": "mobile_money",
        "payment_reference": payment_reference,
        "paystack_reference": payment_reference,
        "charged_amount": round(paystack_totals["paystack_total"], 2),
        "source": {
            "type": "ussd",
            "provider": "arkesel",
            "agent_code": session_doc.get("agent_code"),
            "agent_user_id": session_doc.get("agent_user_id"),
            "agent_identity": agent_identity,
            "session_id": session_id,
            "service_code": service_code,
        },
        "gateway_fee_overage_ghs": round(paystack_totals["fee"], 2),
        "user_id": None,
    }
    _save_pending_payment(
        {
            "kind": "store",
            "status": "charge_initiating",
            "payment_status": "pending",
            "order_id": order_id,
            "payment_reference": payment_reference,
            "paystack_reference": payment_reference,
            "amount": round(paystack_totals["paystack_total"], 2),
            "base_amount": round(paystack_totals["base_total"], 2),
            "gateway_fee_overage_ghs": round(paystack_totals["fee"], 2),
            "currency": "GHS",
            "channel": "arkesel_ussd",
            "agent_code": session_doc.get("agent_code"),
            "agent_user_id": session_doc.get("agent_user_id"),
            "agent_identity": agent_identity,
            "session_id": session_id,
            "service_code": service_code,
            "payer_phone": payer_phone,
            "recipient_phone": recipient_phone,
            "place_payload": place_payload,
        }
    )
    ok, charge_response, reason = _start_paystack_momo_charge(
        order_id=order_id,
        amount_ghs=paystack_totals["paystack_total"],
        payer_phone=payer_phone,
        agent_code=str(session_doc.get("agent_code") or ""),
        agent_identity=agent_identity,
        session_id=session_id,
        reference=payment_reference,
    )
    charge_data = charge_response.get("data") if isinstance(charge_response.get("data"), dict) else {}
    final_reference = charge_data.get("reference") or payment_reference
    if not ok:
        _mark_payment_start_failed(order_id, charge_response, reason)
        return {"success": False, "message": "Payment could not be started. Please try again later.", "order_id": order_id}, 502

    next_step = _handle_paystack_charge_status(order_id, session_id, charge_response)
    if not next_step.get("success"):
        return {"success": False, "message": next_step.get("message") or "Payment failed.", "order_id": order_id}, 502
    return {
        "success": True,
        "order_id": order_id,
        "payment_reference": final_reference,
        "payment_status": "pending" if next_step.get("status") != "processing" else "paid",
        "status": next_step.get("status"),
        "session_status": next_step.get("session_status"),
        "message": next_step.get("message"),
        "keep_open": bool(next_step.get("keep_open")),
    }, 200


def _place_public_ussd_order(
    session_doc: Dict[str, Any],
    selected_service: Dict[str, Any],
    selected_offer: Dict[str, Any],
    recipient_phone: str,
    payer_phone: str,
    session_id: str,
    service_code: str,
):
    from index import _reprice_public_cart

    agent_identity = _session_agent_identity(session_doc)
    amount = float(selected_offer.get("total") or selected_offer.get("amount") or 0)
    label = selected_offer.get("value_text") or "-"
    service_name = selected_service.get("name") or "Service"
    cart_item = {
        "serviceId": str(selected_service.get("_id")),
        "serviceName": service_name,
        "service_network": selected_service.get("service_network"),
        "network": selected_service.get("network"),
        "phone": recipient_phone,
        "value": label,
        "value_obj": selected_offer.get("value"),
        "base_amount": amount,
        "amount": amount,
        "provider": selected_service.get("provider"),
    }

    server_cart, total_requested = _reprice_public_cart([cart_item])
    paystack_totals = _calc_paystack_totals(total_requested)
    order_id = generate_order_id()
    reference = _ussd_paystack_reference(order_id)
    source = {
        "type": "ussd",
        "provider": "arkesel",
        "agent_code": session_doc.get("agent_code"),
        "agent_user_id": session_doc.get("agent_user_id"),
        "agent_identity": agent_identity,
        "session_id": session_id,
        "service_code": service_code,
        "target": "public_index",
        "store_name": "Sir Hans",
    }
    _save_pending_payment(
        {
            "kind": "public",
            "status": "charge_initiating",
            "payment_status": "pending",
            "order_id": order_id,
            "payment_reference": reference,
            "paystack_reference": reference,
            "amount": round(float(paystack_totals["paystack_total"] or 0), 2),
            "base_amount": round(float(paystack_totals["base_total"] or 0), 2),
            "gateway_fee_overage_ghs": round(float(paystack_totals["fee"] or 0), 2),
            "currency": "GHS",
            "channel": "arkesel_ussd",
            "agent_code": session_doc.get("agent_code"),
            "agent_user_id": session_doc.get("agent_user_id"),
            "agent_identity": agent_identity,
            "session_id": session_id,
            "service_code": service_code,
            "payer_phone": payer_phone,
            "recipient_phone": recipient_phone,
            "server_cart": server_cart,
            "total_requested": round(float(total_requested or 0), 2),
            "paystack_total_requested": round(float(paystack_totals["paystack_total"] or 0), 2),
            "source": source,
        }
    )

    ok, charge_response, reason = _start_paystack_momo_charge(
        order_id=order_id,
        amount_ghs=float(paystack_totals["paystack_total"] or 0),
        payer_phone=payer_phone,
        agent_code=str(session_doc.get("agent_code") or ""),
        agent_identity=agent_identity,
        session_id=session_id,
        reference=reference,
    )
    charge_data = charge_response.get("data") if isinstance(charge_response.get("data"), dict) else {}
    final_reference = charge_data.get("reference") or reference
    if not ok:
        _mark_payment_start_failed(order_id, charge_response, reason)
        return {"success": False, "message": "Payment could not be started. Please try again later.", "order_id": order_id}, 502

    next_step = _handle_paystack_charge_status(order_id, session_id, charge_response)
    if not next_step.get("success"):
        return {"success": False, "message": next_step.get("message") or "Payment failed.", "order_id": order_id}, 502

    return {
        "success": True,
        "message": next_step.get("message") or "Payment prompt sent. Approve it on your phone to complete order.",
        "order_id": order_id,
        "status": next_step.get("status") or "awaiting_payment",
        "payment_status": "pending" if next_step.get("status") != "processing" else "paid",
        "payment_reference": final_reference,
        "session_status": next_step.get("session_status"),
        "keep_open": bool(next_step.get("keep_open")),
    }, 200


def _handle_arkesel_json(data: Dict[str, Any]):
    session_id = _first(data, "sessionID", "sessionId", "session_id")
    user_id = _first(data, "userID", "user_id")
    msisdn_raw = _first(data, "msisdn", "phoneNumber", "phone_number")
    service_code = _first(data, "serviceCode", "service_code")
    user_data = _first(data, "userData", "user_data")
    network = _first(data, "network")
    new_session = _is_true(data.get("newSession"))

    if not session_id:
        return _json_reply("", user_id, msisdn_raw, "Missing session", False)

    if new_session:
        resumed = _resume_pending_payment_session(session_id, user_id, msisdn_raw, service_code, network)
        if resumed:
            return _json_reply(
                session_id,
                user_id,
                msisdn_raw,
                resumed["message"],
                bool(resumed["keep_open"]),
            )
        recent = _find_recent_agent(msisdn_raw)
        recent_code = str((recent or {}).get("agent_code") or "").strip()
        if recent_code and _find_agent_code(recent_code):
            _upsert_session(
                session_id,
                {
                    "user_id": user_id,
                    "service_code": service_code,
                    "msisdn": msisdn_raw,
                    "network": network,
                    "user_data": user_data,
                    "recent_agent_code": recent_code,
                    "status": "confirming_recent_agent",
                },
            )
            return _json_reply(
                session_id,
                user_id,
                msisdn_raw,
                f"Use recent Agent Code ({recent_code})?\n1. Yes\n2. No",
                True,
            )
        if recent_code:
            _forget_recent_agent(msisdn_raw)
        _upsert_session(
            session_id,
            {
                "user_id": user_id,
                "service_code": service_code,
                "msisdn": msisdn_raw,
                "network": network,
                "user_data": user_data,
                "status": "awaiting_agent_code",
            },
        )
        return _json_reply(session_id, user_id, msisdn_raw, "Enter Agent Code", True)

    session_doc = ussd_sessions_col.find_one({"session_id": session_id}) or {}
    status = session_doc.get("status") or "awaiting_agent_code"
    _upsert_session(
        session_id,
        {
            "user_id": user_id,
            "service_code": service_code,
            "msisdn": msisdn_raw,
            "network": network,
            "user_data": user_data,
            "last_input": user_data,
        },
    )

    if status == "confirming_recent_agent":
        if user_data == "2":
            _upsert_session(session_id, {"status": "awaiting_agent_code", "recent_agent_code": None})
            return _json_reply(session_id, user_id, msisdn_raw, "Enter Agent Code", True)
        if user_data != "1":
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid option\n1. Yes\n2. No", True)

        recent_code = str(session_doc.get("recent_agent_code") or "").strip()
        context, error = _activate_agent_session(session_id, msisdn_raw, recent_code)
        if not context:
            _forget_recent_agent(msisdn_raw)
            _upsert_session(session_id, {"status": "awaiting_agent_code", "recent_agent_code": None})
            return _json_reply(
                session_id,
                user_id,
                msisdn_raw,
                f"{error or 'Recent agent code is unavailable'}. Enter Agent Code",
                True,
            )
        return _json_reply(
            session_id,
            user_id,
            msisdn_raw,
            _service_menu(context["services"], context["store_doc"].get("name") or "Store"),
            True,
        )

    if status == "awaiting_agent_code":
        context, error = _activate_agent_session(session_id, msisdn_raw, user_data)
        if not context:
            error_status = "store_not_found" if error == "Agent store not found" else "no_services" if error == "No services available" else "invalid_agent_code"
            _upsert_session(session_id, {"status": error_status, "agent_code": user_data})
            return _json_reply(session_id, user_id, msisdn_raw, error or "Invalid agent code", False)
        return _json_reply(
            session_id,
            user_id,
            msisdn_raw,
            _service_menu(context["services"], context["store_doc"].get("name") or "Store"),
            True,
        )

    if status == "awaiting_paystack_otp":
        otp_result = _handle_paystack_otp_input(session_id, user_data)
        return _json_reply(
            session_id,
            user_id,
            msisdn_raw,
            otp_result.get("message") or "Payment failed.",
            bool(otp_result.get("keep_open")),
        )

    is_public_flow = session_doc.get("flow") == "public_index"
    store_doc = _public_store_doc() if is_public_flow else _session_store(session_doc)
    if not store_doc:
        _upsert_session(session_id, {"status": "store_not_found"})
        return _json_reply(session_id, user_id, msisdn_raw, "Agent store not found", False)

    services = _load_public_services() if is_public_flow else _load_store_services(store_doc)
    if status == "selecting_service":
        service_choice = _service_choice(user_data, services)
        if service_choice is None:
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid service", False)
        if service_choice == "contact":
            return _json_reply(
                session_id,
                user_id,
                msisdn_raw,
                _contact_message(store_doc, session_doc.get("agent_user_id")),
                False,
            )
        if service_choice == "check_status":
            _upsert_session(session_id, {"status": "selecting_status_phone"})
            return _json_reply(session_id, user_id, msisdn_raw, "Check Order Status\n1. Self\n2. Other", True)
        service_idx = int(service_choice)
        selected_service = services[service_idx]
        offers = selected_service.get("offers") or []
        if not offers:
            return _json_reply(session_id, user_id, msisdn_raw, "No offers available", False)
        _upsert_session(
            session_id,
            {
                "selected_service_id": selected_service.get("_id"),
                "selected_offer_page": 0,
                "status": "selecting_offer",
            },
        )
        return _json_reply(session_id, user_id, msisdn_raw, _offer_page_menu(offers, 0), True)

    if status == "selecting_status_phone":
        if user_data not in ("1", "2"):
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid option", False)
        if user_data == "1":
            return _json_reply(
                session_id,
                user_id,
                msisdn_raw,
                _latest_order_status_message(_normalize_msisdn(msisdn_raw), session_doc, store_doc),
                False,
            )
        _upsert_session(session_id, {"status": "awaiting_status_phone"})
        return _json_reply(session_id, user_id, msisdn_raw, "Enter phone number", True)

    if status == "awaiting_status_phone":
        normalized_phone = _normalize_msisdn(user_data)
        if not normalized_phone:
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid phone number", False)
        return _json_reply(
            session_id,
            user_id,
            msisdn_raw,
            _latest_order_status_message(normalized_phone, session_doc, store_doc),
            False,
        )

    store_doc, selected_service, selected_offer, _services = _selected_context(session_doc)
    if not store_doc or not selected_service:
        return _json_reply(session_id, user_id, msisdn_raw, "Session expired. Try again.", False)

    offers = selected_service.get("offers") or []
    if status == "selecting_offer":
        offer_page = int(session_doc.get("selected_offer_page") or 0)
        offer_choice = _offer_choice(user_data, offers, offer_page)
        if offer_choice is None:
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid offer", False)
        if offer_choice == "next_page":
            next_page = offer_page + 1
            _upsert_session(session_id, {"selected_offer_page": next_page})
            return _json_reply(session_id, user_id, msisdn_raw, _offer_page_menu(offers, next_page), True)
        if offer_choice == "prev_page":
            prev_page = max(offer_page - 1, 0)
            _upsert_session(session_id, {"selected_offer_page": prev_page})
            return _json_reply(session_id, user_id, msisdn_raw, _offer_page_menu(offers, prev_page), True)
        _upsert_session(
            session_id,
            {
                "selected_offer_index": int(offer_choice),
                "status": "selecting_recipient",
            },
        )
        return _json_reply(session_id, user_id, msisdn_raw, "Recipient\n1. Self\n2. Other", True)

    if status == "selecting_recipient":
        if user_data not in ("1", "2"):
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid recipient", False)
        if user_data == "2":
            _upsert_session(session_id, {"recipient_choice": "other", "status": "awaiting_recipient_phone"})
            return _json_reply(session_id, user_id, msisdn_raw, "Enter recipient number", True)

        selected_offer = offers[int(session_doc.get("selected_offer_index"))]
        recipient_phone = _normalize_msisdn(msisdn_raw)
        _upsert_session(
            session_id,
            {
                "recipient_choice": "self",
                "recipient_phone": recipient_phone,
                "status": "awaiting_confirmation",
            },
        )
        return _json_reply(
            session_id,
            user_id,
            msisdn_raw,
            _confirm_message(
                selected_service.get("name") or "Service",
                selected_offer.get("value_text") or "-",
                float(selected_offer.get("total") or 0),
                recipient_phone,
            ),
            True,
        )

    if status == "awaiting_recipient_phone":
        recipient_phone = _normalize_msisdn(user_data)
        if not recipient_phone:
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid recipient number", False)
        _upsert_session(
            session_id,
            {
                "pending_recipient_phone": recipient_phone,
                "status": "awaiting_recipient_phone_confirmation",
            },
        )
        return _json_reply(session_id, user_id, msisdn_raw, "Enter recipient number again to confirm", True)

    if status == "awaiting_recipient_phone_confirmation":
        first_phone = _normalize_msisdn(str(session_doc.get("pending_recipient_phone") or ""))
        confirmed_phone = _normalize_msisdn(user_data)
        if not confirmed_phone:
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid recipient number. Enter it again", True)
        if not first_phone or confirmed_phone != first_phone:
            _upsert_session(
                session_id,
                {
                    "pending_recipient_phone": None,
                    "recipient_phone": None,
                    "status": "awaiting_recipient_phone",
                },
            )
            return _json_reply(
                session_id,
                user_id,
                msisdn_raw,
                "Numbers do not match. Enter recipient number again",
                True,
            )

        recipient_phone = confirmed_phone
        selected_offer = offers[int(session_doc.get("selected_offer_index"))]
        _upsert_session(
            session_id,
            {
                "recipient_phone": recipient_phone,
                "pending_recipient_phone": None,
                "status": "awaiting_confirmation",
            },
        )
        return _json_reply(
            session_id,
            user_id,
            msisdn_raw,
            _confirm_message(
                selected_service.get("name") or "Service",
                selected_offer.get("value_text") or "-",
                float(selected_offer.get("total") or 0),
                recipient_phone,
            ),
            True,
        )

    if status == "awaiting_confirmation":
        if user_data == "2":
            _upsert_session(session_id, {"status": "cancelled"})
            return _json_reply(session_id, user_id, msisdn_raw, "Order cancelled", False)
        if user_data != "1":
            return _json_reply(session_id, user_id, msisdn_raw, "Invalid option", False)

        if not selected_offer:
            return _json_reply(session_id, user_id, msisdn_raw, "Session expired. Try again.", False)

        recipient_phone = session_doc.get("recipient_phone") or _normalize_msisdn(msisdn_raw)
        if _ussd_known_number_ineligible(selected_service, recipient_phone):
            _upsert_session(session_id, {"status": "ineligible"})
            return _json_reply(session_id, user_id, msisdn_raw, "Ineligible", False)
        if session_doc.get("flow") == "public_index":
            result, status_code = _place_public_ussd_order(
                session_doc,
                selected_service,
                selected_offer,
                recipient_phone,
                _normalize_msisdn(msisdn_raw),
                session_id,
                service_code,
            )
        else:
            result, status_code = _place_ussd_order(
                session_doc,
                store_doc,
                selected_service,
                selected_offer,
                recipient_phone,
                _normalize_msisdn(msisdn_raw),
                session_id,
                service_code,
            )
        if not result.get("success"):
            _upsert_session(session_id, {"status": "failed", "failure": result.get("message")})
            return _json_reply(session_id, user_id, msisdn_raw, result.get("message") or "Order failed", False)

        _upsert_session(
            session_id,
            {
                "status": result.get("session_status") or ("awaiting_paystack_otp" if result.get("keep_open") else "completed"),
                "order_id": result.get("order_id"),
                "payment_reference": result.get("payment_reference"),
                "place_status_code": status_code,
            },
        )
        return _json_reply(
            session_id,
            user_id,
            msisdn_raw,
            result.get("message") or "Payment prompt sent. Approve on your phone.",
            bool(result.get("keep_open")),
        )

    return _json_reply(session_id, user_id, msisdn_raw, "Session expired. Try again.", False)


def _handle_text_flow(data: Dict[str, Any]):
    session_id = _first(data, "sessionId", "session_id")
    service_code = _first(data, "serviceCode", "service_code")
    msisdn_raw = _first(data, "phoneNumber", "msisdn")
    text = _first(data, "text")
    raw_parts = text.split("*") if text else []
    parts = list(raw_parts)

    if not session_id:
        return _plain("END Missing session")

    if not text:
        resumed = _resume_pending_payment_session(session_id, "", msisdn_raw, service_code, "")
        if resumed:
            prefix = "CON" if resumed["keep_open"] else "END"
            return _plain(f"{prefix} {resumed['message']}")
        recent = _find_recent_agent(msisdn_raw)
        recent_code = str((recent or {}).get("agent_code") or "").strip()
        if recent_code and _find_agent_code(recent_code):
            _upsert_session(
                session_id,
                {
                    "service_code": service_code,
                    "msisdn": msisdn_raw,
                    "recent_agent_code": recent_code,
                    "status": "confirming_recent_agent_text",
                },
            )
            return _plain(f"CON Use recent Agent Code ({recent_code})?\n1. Yes\n2. No")
        if recent_code:
            _forget_recent_agent(msisdn_raw)

    _upsert_session(session_id, {"service_code": service_code, "msisdn": msisdn_raw, "text": text})
    existing_session = ussd_sessions_col.find_one({"session_id": session_id}) or {}
    agent_prefix_code = str(existing_session.get("text_agent_prefix_code") or "").strip()
    agent_input_offset = existing_session.get("text_agent_input_offset")
    if agent_prefix_code and isinstance(agent_input_offset, int) and agent_input_offset >= 0:
        parts = [agent_prefix_code] + raw_parts[agent_input_offset:]

    retry_prefix = existing_session.get("text_recipient_retry_prefix")
    retry_at = existing_session.get("text_recipient_retry_at")
    if isinstance(retry_prefix, list) and isinstance(retry_at, int) and retry_at >= 0:
        parts = [str(value) for value in retry_prefix] + raw_parts[retry_at:]

    if existing_session.get("status") == "awaiting_paystack_otp":
        otp_value = parts[-1] if parts else text
        otp_result = _handle_paystack_otp_input(session_id, otp_value)
        prefix = "CON" if otp_result.get("keep_open") else "END"
        return _plain(f"{prefix} {otp_result.get('message') or 'Payment failed.'}")

    if existing_session.get("status") == "confirming_recent_agent_text":
        choice = raw_parts[-1] if raw_parts else ""
        if choice == "2":
            _upsert_session(session_id, {"status": "awaiting_new_agent_code_text", "recent_agent_code": None})
            return _plain("CON Enter Agent Code")
        if choice != "1":
            return _plain("CON Invalid option\n1. Yes\n2. No")
        recent_code = str(existing_session.get("recent_agent_code") or "").strip()
        if not recent_code or not _find_agent_code(recent_code):
            _forget_recent_agent(msisdn_raw)
            _upsert_session(session_id, {"status": "awaiting_new_agent_code_text", "recent_agent_code": None})
            return _plain("CON Recent agent code is unavailable. Enter Agent Code")
        ussd_sessions_col.update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "text_agent_prefix_code": recent_code,
                    "text_agent_input_offset": len(raw_parts),
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        parts = [recent_code]

    elif existing_session.get("status") == "awaiting_new_agent_code_text":
        entered_new_code = raw_parts[-1] if raw_parts else ""
        ussd_sessions_col.update_one(
            {"session_id": session_id},
            {
                "$set": {
                    "text_agent_prefix_code": entered_new_code,
                    "text_agent_input_offset": len(raw_parts),
                    "updated_at": datetime.utcnow(),
                }
            },
        )
        parts = [entered_new_code]

    if len(parts) == 0:
        return _plain("CON Enter Agent Code")

    entered_code = (parts[0] or "").strip()
    context, error = _activate_agent_session(session_id, msisdn_raw, entered_code)
    if not context:
        return _plain(f"END {error or 'Invalid agent code'}")
    is_public_flow = bool(context["is_public"])
    agent_user_id = context["agent_user_id"]
    agent_identity = context["agent_identity"]
    store_doc = context["store_doc"]
    services = context["services"]

    if len(parts) == 1:
        rows = [f"{i}. {svc.get('name')}" for i, svc in enumerate(services[:8], start=1)]
        rows.append(f"{len(rows) + 1}. Contact Us")
        rows.append(f"{len(rows) + 1}. Check Order Status")
        return _plain(_menu(f"Welcome {store_doc.get('name') or 'Store'}\nSelect Service", rows))

    service_choice = _service_choice(parts[1], services)
    if service_choice is None:
        return _plain("END Invalid service")
    if service_choice == "contact":
        return _plain("END " + _contact_message(store_doc, agent_user_id))
    if service_choice == "check_status":
        if len(parts) == 2:
            return _plain("CON Check Order Status\n1. Self\n2. Other")
        if parts[2] not in ("1", "2"):
            return _plain("END Invalid option")
        if parts[2] == "1":
            return _plain("END " + _latest_order_status_message(_normalize_msisdn(msisdn_raw), {"flow": "public_index" if is_public_flow else "store", "store_slug": store_doc.get("slug")}, store_doc))
        if len(parts) == 3:
            return _plain("CON Enter phone number")
        return _plain("END " + _latest_order_status_message(_normalize_msisdn(parts[3]), {"flow": "public_index" if is_public_flow else "store", "store_slug": store_doc.get("slug")}, store_doc))
    service_idx = int(service_choice)
    selected_service = services[service_idx]
    offers = selected_service.get("offers") or []
    if len(parts) == 2:
        return _plain("CON " + _offer_page_menu(offers, 0))

    offer_page = 0
    page_cursor = 2
    while True:
        if len(parts) <= page_cursor:
            return _plain("CON " + _offer_page_menu(offers, offer_page))
        offer_choice = _offer_choice(parts[page_cursor], offers, offer_page)
        if offer_choice is None:
            return _plain("END Invalid offer")
        if offer_choice == "next_page":
            offer_page += 1
            page_cursor += 1
            continue
        if offer_choice == "prev_page":
            offer_page = max(offer_page - 1, 0)
            page_cursor += 1
            continue
        offer_idx = int(offer_choice)
        break

    if offer_idx is None:
        return _plain("END Invalid offer")
    selected_offer = offers[offer_idx]

    if len(parts) == page_cursor + 1:
        return _plain("CON Recipient\n1. Self\n2. Other")

    recipient_choice = parts[page_cursor + 1]
    if recipient_choice not in ("1", "2"):
        return _plain("END Invalid recipient")

    if recipient_choice == "1":
        recipient_phone = _normalize_msisdn(msisdn_raw)
        next_part_index = page_cursor + 2
    else:
        if len(parts) == page_cursor + 2:
            return _plain("CON Enter recipient number")
        first_phone = _normalize_msisdn(parts[page_cursor + 2])
        if not first_phone:
            return _plain("CON Invalid recipient number. Enter recipient number again")
        if len(parts) == page_cursor + 3:
            return _plain("CON Enter recipient number again to confirm")
        confirmed_phone = _normalize_msisdn(parts[page_cursor + 3])
        if not confirmed_phone or confirmed_phone != first_phone:
            retry_prefix = parts[: page_cursor + 2]
            ussd_sessions_col.update_one(
                {"session_id": session_id},
                {
                    "$set": {
                        "text_recipient_retry_prefix": retry_prefix,
                        "text_recipient_retry_at": len(raw_parts),
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
            return _plain("CON Numbers do not match. Enter recipient number again")
        recipient_phone = confirmed_phone
        next_part_index = page_cursor + 4

    amount = float(selected_offer.get("total") or 0)
    label = selected_offer.get("value_text") or "-"
    service_name = selected_service.get("name") or "Service"
    if len(parts) == next_part_index:
        return _plain("CON " + _confirm_message(service_name, label, amount, recipient_phone))

    confirm = parts[next_part_index]
    if confirm == "2":
        _upsert_session(session_id, {"status": "cancelled"})
        return _plain("END Order cancelled")
    if confirm != "1":
        return _plain("END Invalid option")
    if _ussd_known_number_ineligible(selected_service, recipient_phone):
        _upsert_session(session_id, {"status": "ineligible"})
        return _plain("END Ineligible")

    session_doc = {
        "agent_code": entered_code,
        "agent_user_id": agent_user_id,
        "agent_identity": agent_identity,
        "flow": "public_index" if is_public_flow else "store",
    }
    if is_public_flow:
        result, status_code = _place_public_ussd_order(
            session_doc,
            selected_service,
            selected_offer,
            recipient_phone,
            _normalize_msisdn(msisdn_raw),
            session_id,
            service_code,
        )
    else:
        result, status_code = _place_ussd_order(
            session_doc,
            store_doc,
            selected_service,
            selected_offer,
            recipient_phone,
            _normalize_msisdn(msisdn_raw),
            session_id,
            service_code,
        )
    if not result.get("success"):
        return _plain(f"END {result.get('message') or 'Order failed'}")
    _upsert_session(
        session_id,
        {
            "status": result.get("session_status") or ("awaiting_paystack_otp" if result.get("keep_open") else "completed"),
            "order_id": result.get("order_id"),
            "payment_reference": result.get("payment_reference"),
            "place_status_code": status_code,
        },
    )
    prefix = "CON" if result.get("keep_open") else "END"
    return _plain(f"{prefix} {result.get('message') or 'Payment prompt sent. Approve on your phone.'}")


@arkesel_ussd_bp.route("/ussd/arkesel/callback", methods=["POST"])
def arkesel_callback():
    data = _payload()
    session_id = _first(data, "sessionID", "sessionId", "session_id")
    user_id = _first(data, "userID", "user_id")
    msisdn_raw = _first(data, "msisdn", "phoneNumber", "phone_number")

    try:
        if any(key in data for key in ("sessionID", "userID", "newSession", "userData", "network")):
            response = _handle_arkesel_json(data)
        else:
            response = _handle_text_flow(data)

        response_body = response.get_json(silent=True) if hasattr(response, "get_json") else None
        _log_ussd_request(data, response_body=response_body)
        return response
    except Exception as exc:
        response_body = _json_body(session_id, user_id, msisdn_raw, "Service temporarily unavailable. Try again later.", False)
        _log_ussd_request(data, response_body=response_body, error=str(exc))
        print(f"[arkesel_ussd] callback error: {exc}")
        return jsonify(response_body)
