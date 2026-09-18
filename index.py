# index.py — Public landing page ONLY (no checkout / no orders)
from __future__ import annotations

from flask import Blueprint, render_template, jsonify, request, abort, send_from_directory
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import json, ast, re, os, threading, uuid
import requests
from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from announcement_utils import get_active_announcements
from db import db
from deposit import PAYSTACK_PUBLIC_KEY as DEPOSIT_PAYSTACK_PK
from deposit import PAYSTACK_SECRET_KEY as DEPOSIT_PAYSTACK_SK

index_bp = Blueprint("index", __name__)

# --- DB collections ---
services_col = db["services"]
orders_col = db["orders"]
transactions_col = db["transactions"]
payment_sessions_col = db["payment_sessions"]
wassce_col = db["wassce_checker"]
purchase_history_col = db["purchase_history"]

RESULTS_CHECKER_SERVICE_ID = "results_checker_service"
RESULTS_CHECKER_NAME = "Results Checker"
RESULTS_CHECKER_IMAGE_URL = "/images/checker.png"

try:
    from admin_balance import _send_sms as _send_results_checker_sms
    from admin_balance import _normalize_phone as _normalize_results_checker_phone
except Exception:  # pragma: no cover
    _send_results_checker_sms = None
    _normalize_results_checker_phone = None

# (Optional) still load Paystack public key if your index.html references it in JS
PAYSTACK_PUBLIC_KEY = DEPOSIT_PAYSTACK_PK or os.getenv("PAYSTACK_PUBLIC_KEY", "")
PAYSTACK_SECRET_KEY = DEPOSIT_PAYSTACK_SK or os.getenv("PAYSTACK_SECRET_KEY", "")
PUBLIC_PAYSTACK_FEE_RATE = 0.02

try:
    payment_sessions_col.create_index([("reference", 1)], unique=True)
    payment_sessions_col.create_index([("updated_at", -1)])
    orders_col.create_index(
        [("paystack_reference", 1)],
        unique=True,
        partialFilterExpression={"paystack_reference": {"$exists": True, "$type": "string"}},
    )
except Exception:
    pass

# Store-host guard: hansmart.store should not serve the public landing page
STORE_PUBLIC_HOST = (os.getenv("STORE_PUBLIC_HOST", "www.hansmart.store") or "").strip().lower()
_STORE_HOSTS = {STORE_PUBLIC_HOST, STORE_PUBLIC_HOST.lstrip("www.")}

def _host_only(v: str) -> str:
    return (v or "").split(":", 1)[0].strip().lower()

try:
    from checkout import (  # type: ignore
        _coerce_value_obj,
        _resolve_network_id,
        _resolve_network_slug,
        _resolve_codecraft_network,
        _resolve_codecraft_gig,
        _codecraft_get_packages_cached,
        _resolve_package_size_gb,
        _resolve_skplug_network,
        _resolve_shared_bundle_mb,
        _resolve_datakazina_shared_bundle,
        _background_process_providers,
        _service_unavailability_reason,
        _build_bundle_key,
        _has_processing_conflict_strict,
        _known_number_enforcement_enabled,
        _known_number_validation_error,
        _service_requires_known_number_verification,
        generate_order_id,
        _money,
        jlog,
    )
except Exception:  # pragma: no cover
    from .checkout import (  # type: ignore
        _coerce_value_obj,
        _resolve_network_id,
        _resolve_network_slug,
        _resolve_codecraft_network,
        _resolve_codecraft_gig,
        _codecraft_get_packages_cached,
        _resolve_package_size_gb,
        _resolve_skplug_network,
        _resolve_shared_bundle_mb,
        _resolve_datakazina_shared_bundle,
        _background_process_providers,
        _service_unavailability_reason,
        _build_bundle_key,
        _has_processing_conflict_strict,
        _known_number_enforcement_enabled,
        _known_number_validation_error,
        _service_requires_known_number_verification,
        generate_order_id,
        _money,
        jlog,
    )

# ---------------- small helpers (local, no checkout imports) ----------------

def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None

def _money(v: Any) -> float:
    """Simple money normalizer -> 2dp float."""
    try:
        return round(float(v or 0.0), 2)
    except Exception:
        return 0.0

_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)
_mapping_like = re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)

def _service_unit(svc: Dict[str, Any]) -> str:
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes"):
        return "minutes"
    if name == "afa talktime":
        return "minutes"
    return "data"

def _parse_value_field(value: Any) -> Any:
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        vt = value.strip()
        if vt.startswith("{") and vt.endswith("}"):
            try:
                data = json.loads(vt)
                if isinstance(data, dict):
                    return data
            except Exception:
                try:
                    if _mapping_like.match(vt):
                        data = ast.literal_eval(vt)
                        if isinstance(data, dict):
                            return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    """
    For unit == 'data' -> we treat volume as MB.
    For unit == 'minutes' -> we treat volume as minutes.
    """
    if isinstance(value, dict):
        vol = value.get("volume")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            return float(vol)
        vol_s = str(vol)
        if unit == "minutes":
            m = _MIN.search(vol_s)
            if m: return float(m.group(1))
            if _NUM.match(vol_s): return float(vol_s)
            return None
        else:
            m = _GB.search(vol_s)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(vol_s)
            if m: return float(m.group(1))
            if _NUM.match(vol_s): return float(vol_s)
            return None

    if isinstance(value, str):
        s = value
        if unit == "minutes":
            m = _MIN.search(s)
            if m: return float(m.group(1))
            if _NUM.match(s): return float(s)
            s2 = _PKG_TAIL.sub("", s)
            m = _MIN.search(s2)
            if m: return float(m.group(1))
            return None
        else:
            m = _GB.search(s)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(s)
            if m: return float(m.group(1))
            s2 = _PKG_TAIL.sub("", s)
            m = _GB.search(s2)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(s2)
            if m: return float(m.group(1))
            if _NUM.match(s2): return float(s2)
            return None

    return None

def _format_volume_unit(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if unit == "minutes":
        return f"{int(round(v))} mins"
    if v >= 1000:
        gb = v / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(v)}MB"

def _value_text_for_display(value: Any, unit: str) -> str:
    if isinstance(value, dict):
        vol = _extract_volume(value, unit)
        return _format_volume_unit(vol, unit) if vol is not None else "-"
    if isinstance(value, str):
        cleaned = _PKG_TAIL.sub("", value).strip()
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"

def _norm(s: str) -> str:
    return (s or "").strip().lower()

PREFERRED_ORDER: List[str] = ["MTN NORMAL", "MTN EXPRESS", "MTN", "AT - iShare", "AT - BigTime", "AFA TALKTIME"]

def _name_rank(name: str) -> Optional[int]:
    n = _norm(name)
    for i, want in enumerate(PREFERRED_ORDER):
        if _norm(want) == n:
            return i
    n2 = " ".join(n.split())
    for i, want in enumerate(PREFERRED_ORDER):
        if " ".join(_norm(want).split()) == n2:
            return i
    return None

def _created_ts(service_doc: Dict[str, Any]) -> float:
    ca = service_doc.get("created_at")
    if isinstance(ca, datetime):
        return ca.timestamp()
    try:
        val = float(ca)
        if val > 1e12:
            return val / 1000.0
        return val
    except Exception:
        return 0.0

def _service_priority_tuple(svc: Dict[str, Any]):
    prio = _to_float(svc.get("priority"))
    prio = prio if prio is not None else float("inf")
    name = svc.get("name") or ""
    nrank = _name_rank(name)
    nrank = nrank if nrank is not None else 10_000
    display_order = _to_float(svc.get("display_order"))
    display_order = display_order if display_order is not None else float("inf")
    ts = -_created_ts(svc)
    alpha = _norm(name)
    return (prio, nrank, display_order, ts, alpha)

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()
    availability = (svc.get("availability") or "AVAILABLE").upper()
    closed_msg = (svc.get("closed_message") or "This service is temporarily closed.")
    oos_msg = (svc.get("out_of_stock_message") or "This service is currently out of stock.")
    can_order = (status == "OPEN" and availability == "AVAILABLE")
    disabled_reason = None
    if not can_order:
        if status != "OPEN":
            disabled_reason = closed_msg
        elif availability != "AVAILABLE":
            disabled_reason = oos_msg
        else:
            disabled_reason = "This service is currently unavailable."
    return {
        "type": t,
        "status": status,
        "availability": availability,
        "closed_message": closed_msg,
        "out_of_stock_message": oos_msg,
        "can_order": can_order,
        "disabled_reason": disabled_reason,
    }

def _public_offers_list(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    public_offers = svc.get("public_offers")
    if isinstance(public_offers, list) and public_offers:
        return public_offers
    offers = svc.get("offers")
    if isinstance(offers, list) and offers:
        return offers
    return []

def _offer_base_amount(of: Dict[str, Any]) -> Optional[float]:
    if not isinstance(of, dict):
        return None
    return _to_float(of.get("amount"))

def _canonical_public_total_for_offer(
    svc_doc: Dict[str, Any],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    offers = _public_offers_list(svc_doc)
    if not offers:
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")
    for idx, of in enumerate(offers):
        parsed = _parse_value_field(of.get("value"))
        vol = _extract_volume(parsed, unit)
        if vol_needed is not None and vol is not None:
            diff = abs(float(vol) - float(vol_needed))
            if diff < best_diff:
                best_idx, best_diff = idx, diff
        elif best_idx is None:
            best_idx = idx

    if best_idx is None:
        return None
    base_amount = _offer_base_amount(offers[best_idx])
    return round(float(base_amount), 2) if base_amount is not None else None

def _results_checker_type(value_obj: Any, value_raw: Any = None) -> str:
    for candidate in (value_obj, value_raw):
        if isinstance(candidate, dict):
            values = [candidate.get(key) for key in ("type", "checker_type", "id", "value")]
        else:
            values = [candidate]
        for value in values:
            checker_type = str(value or "").strip().lower()
            if checker_type in {"wassce", "bece"}:
                return checker_type
    return ""

def _results_checker_offer(checker_type: str) -> Optional[Dict[str, Any]]:
    if checker_type not in {"wassce", "bece"}:
        return None
    checker = wassce_col.find_one(
        {"type": checker_type, "status": "not_sold"},
        sort=[("created_at", 1)],
    )
    amount = _to_float((checker or {}).get("amount"))
    if not checker or amount is None or amount <= 0:
        return None
    return {
        "amount": round(float(amount), 2),
        "total": round(float(amount), 2),
        "value": {"type": checker_type, "checker_type": checker_type},
        "value_text": checker_type.upper(),
    }

def _build_results_checker_service() -> Dict[str, Any]:
    offers = [offer for checker_type in ("wassce", "bece") if (offer := _results_checker_offer(checker_type))]
    return {
        "_id": RESULTS_CHECKER_SERVICE_ID,
        "_id_str": RESULTS_CHECKER_SERVICE_ID,
        "name": RESULTS_CHECKER_NAME,
        "type": "MANUAL",
        "status": "OPEN" if offers else "CLOSED",
        "availability": "AVAILABLE" if offers else "OUT_OF_STOCK",
        "disabled_reason": None if offers else "Results checker is currently out of stock.",
        "can_order": bool(offers),
        "image_url": RESULTS_CHECKER_IMAGE_URL,
        "service_category": "results_checker",
        "provider": "arkesel_sms",
        "unit": "item",
        "offers": offers,
    }

# ------------------ data prep for landing page ------------------

def load_services_for_landing() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Load all services, normalize offers for display only.
    No wallet, no orders, no external providers.
    """
    exclude_ids = set()
    exclude_names = {"afa talktime"}
    raw = list(services_col.find({"display": {"$ne": "OFF"}, "public_display": {"$ne": "OFF"}}))
    raw.sort(key=_service_priority_tuple)

    services: List[Dict[str, Any]] = []
    for s in raw:
        if _norm(s.get("name") or "") in exclude_names:
            continue
        s = dict(s)
        s["_id_str"] = str(s["_id"])
        st = _service_state(s)
        s.update(st)

        unit = _service_unit(s)
        offers = _public_offers_list(s)

        normalized_offers: List[Dict[str, Any]] = []
        for of in offers:
            parsed_value = _parse_value_field(of.get("value"))
            vol_num = _extract_volume(parsed_value, unit)
            value_text = _value_text_for_display(parsed_value, unit)

            amount = _to_float(of.get("amount"))
            total = amount if amount is not None else None

            normalized_offers.append(
                {
                    "amount": amount,
                    "value": parsed_value,
                    "value_text": value_text,
                    "total": total,
                    "_sort_vol": vol_num if vol_num is not None else float("inf"),
                    "_sort_amt": amount if amount is not None else float("inf"),
                }
            )

        normalized_offers.sort(key=lambda x: (x["_sort_vol"], x["_sort_amt"]))
        s["offers"] = [
            {k: v for k, v in o.items() if not k.startswith("_sort_")}
            for o in normalized_offers
        ]
        s["unit"] = unit

        services.append(s)

    services.append(_build_results_checker_service())
    return services, []


def _verify_paystack(reference: str) -> Tuple[bool, Dict[str, Any], str]:
    if not PAYSTACK_SECRET_KEY or not PAYSTACK_SECRET_KEY.strip():
        return (False, {}, "Payment processor not configured.")
    try:
        headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        r = requests.get(url, headers=headers, timeout=25)
        result = r.json()
        if not result.get("status"):
            return (False, result, result.get("message") or "Verification failed.")
        data = result.get("data") or {}
        ok = data.get("status") == "success"
        if not ok:
            return (False, data, data.get("gateway_response") or "Payment not successful.")
        return (True, data, "")
    except Exception as e:
        return (False, {}, f"Verify error: {str(e)}")

def _paid_enough(paid_pesewas: int, expected_pesewas: int) -> bool:
    return int(paid_pesewas or 0) >= int(expected_pesewas or 0)

def _calc_public_paystack_totals(base_total: float) -> Dict[str, float]:
    base = round(float(base_total or 0.0), 2)
    fee = round(base * PUBLIC_PAYSTACK_FEE_RATE, 2)
    total = round(base + fee, 2)
    return {"base_total": base, "fee": fee, "paystack_total": total}

def _load_payment_session(reference: str) -> Optional[Dict[str, Any]]:
    return payment_sessions_col.find_one({"reference": reference})

def _append_processing_note(reference: str, note: str):
    try:
        payment_sessions_col.update_one(
            {"reference": reference},
            {
                "$push": {"processing_notes": {"at": datetime.utcnow(), "note": note}},
                "$set": {"updated_at": datetime.utcnow()},
            },
        )
    except Exception:
        pass

def _find_public_order_by_reference(reference: str) -> Optional[Dict[str, Any]]:
    if not reference:
        return None
    return orders_col.find_one({"paystack_reference": reference, "paid_from": "public_paystack"})

def _single_order_status_from_line(item: Dict[str, Any], fallback: str = "pending") -> str:
    line_status = str((item or {}).get("line_status") or "").strip().lower()
    if line_status in {"skipped_duplicate_processing", "skipped_duplicate_in_cart"}:
        return "skipped"
    if line_status == "delivered":
        return "delivered"
    if line_status == "failed":
        return "failed"
    return fallback


def _persist_public_split_orders(
    *,
    base_order_fields: Dict[str, Any],
    results: List[Dict[str, Any]],
    api_jobs: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Tuple[str, List[Dict[str, Any]]]]]:
    job_map = {
        str(job.get("provider_request_order_id") or "").strip(): job
        for job in (api_jobs or [])
        if str(job.get("provider_request_order_id") or "").strip()
    }

    created_docs: List[Dict[str, Any]] = []
    order_jobs: List[Tuple[str, List[Dict[str, Any]]]] = []
    group_order_id = str(base_order_fields.get("order_id") or generate_order_id()).strip()

    for idx, raw_item in enumerate(results or [], start=1):
        item = dict(raw_item or {})
        line_ref = str(item.get("provider_request_order_id") or "").strip()
        line_order_id = generate_order_id()
        status = _single_order_status_from_line(item, str(base_order_fields.get("status") or "pending"))

        order_doc = dict(base_order_fields)
        order_doc["order_id"] = line_order_id
        order_doc["items"] = [item]
        order_doc["total_amount"] = round(float(item.get("amount") or 0.0), 2)
        order_doc["charged_amount"] = order_doc["total_amount"] if status != "skipped" else 0.0
        order_doc["profit_amount_total"] = round(float(item.get("profit_amount") or 0.0), 2)
        order_doc["status"] = status

        debug_block = dict(order_doc.get("debug") or {})
        debug_block.update(
            {
                "split_from_bulk_save": True,
                "bulk_group_order_id": group_order_id,
                "bulk_group_index": idx,
                "bulk_group_size": len(results or []),
            }
        )
        order_doc["debug"] = debug_block

        if idx > 1:
            order_doc.pop("paystack_reference", None)

        orders_col.insert_one(order_doc)
        created_docs.append(order_doc)

        if line_ref and line_ref in job_map:
            order_jobs.append((line_order_id, [job_map[line_ref]]))

    return created_docs, order_jobs

def _reprice_public_cart(cart: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], float]:
    checker_lines = [item for item in (cart or []) if item.get("serviceId") == RESULTS_CHECKER_SERVICE_ID]
    if checker_lines and len(cart or []) != 1:
        raise ValueError("Please purchase the Results Checker separately from other services.")

    server_cart: List[Dict[str, Any]] = []
    total_requested = 0.0

    for item in (cart or []):
        service_id_raw = item.get("serviceId")
        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))
        if service_id_raw == RESULTS_CHECKER_SERVICE_ID:
            checker_type = _results_checker_type(value_obj, item.get("value"))
            offer = _results_checker_offer(checker_type)
            if not offer:
                raise ValueError(f"No {checker_type.upper() if checker_type else ''} results checker is currently available.".strip())
            server_item = dict(item)
            server_item.update({
                "serviceId": RESULTS_CHECKER_SERVICE_ID,
                "serviceName": RESULTS_CHECKER_NAME,
                "service_category": "results_checker",
                "value": offer["value"],
                "value_obj": offer["value"],
                "value_text": offer["value_text"],
                "amount": offer["amount"],
            })
            server_cart.append(server_item)
            total_requested += offer["amount"]
            continue
        svc_doc = None
        if service_id_raw:
            try:
                svc_doc = services_col.find_one({
                    "_id": ObjectId(service_id_raw),
                    "display": {"$ne": "OFF"},
                    "public_display": {"$ne": "OFF"},
                })
            except Exception:
                svc_doc = None
            if not svc_doc:
                raise ValueError("This service is not available on the public page.")

        canonical = _canonical_public_total_for_offer(
            svc_doc or {}, value_obj, item.get("value")
        ) if svc_doc else None
        if canonical is None:
            canonical = _money(item.get("amount"))

        server_item = dict(item)
        server_item["amount"] = canonical
        if svc_doc and _service_requires_known_number_verification(server_item, svc_doc):
            server_item["serviceName"] = server_item.get("serviceName") or svc_doc.get("name")
            server_item["service_network"] = server_item.get("service_network") or svc_doc.get("service_network")
            server_item["network"] = server_item.get("network") or svc_doc.get("network")
        server_cart.append(server_item)
        total_requested += canonical

    known_number_error = _known_number_validation_error(server_cart, source="index")
    if known_number_error:
        raise ValueError(str(known_number_error.get("message") or "Number verification failed."))

    return server_cart, round(total_requested, 2)

def _upsert_payment_session(reference: str, cart: List[Dict[str, Any]], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    server_cart, total_requested = _reprice_public_cart(cart)
    totals = _calc_public_paystack_totals(total_requested)
    phones = [str(it.get("phone") or "").strip() for it in cart if str(it.get("phone") or "").strip()]
    now = datetime.utcnow()
    payment_sessions_col.update_one(
        {"reference": reference},
        {
            "$set": {
                "cart_snapshot": cart,
                "server_cart_snapshot": server_cart,
                "customer_phone": phones[0] if phones else "",
                "phones": phones,
                "total_expected": totals["base_total"],
                "paystack_total_expected": totals["paystack_total"],
                "fee_expected": totals["fee"],
                "public_payload": payload or {},
                "updated_at": now,
            },
            "$setOnInsert": {
                "status": "initialized",
                "order_id": None,
                "processing_notes": [],
                "created_at": now,
            },
        },
        upsert=True,
    )
    return payment_sessions_col.find_one({"reference": reference}) or {}

def _ensure_public_transaction(reference: str, paid_ghs: float, total_expected: float, verify_data: Dict[str, Any]) -> None:
    existing = transactions_col.find_one({"reference": reference, "source": "paystack_inline", "status": "success"})
    if existing:
        return
    transactions_col.insert_one(
        {
            "user_id": None,
            "amount": round(paid_ghs, 2),
            "reference": reference,
            "status": "success",
            "type": "debit",
            "source": "paystack_inline",
            "gateway": "Paystack",
            "currency": verify_data.get("currency"),
            "channel": verify_data.get("channel"),
            "verified_at": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "raw": verify_data,
            "meta": {
                "public_checkout": True,
                "expected_pay_total_ghs": total_expected,
                "paid_total_ghs": paid_ghs,
            },
        }
    )

def _build_public_receipt(order_doc: Dict[str, Any], verify_data: Optional[Dict[str, Any]] = None, session_doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    items = order_doc.get("items") or []
    service_summary = []
    phones = []
    for item in items:
        phone = str(item.get("phone") or "").strip()
        service_summary.append(
            {
                "service": str(item.get("serviceName") or "Service").strip(),
                "offer": str(item.get("value") or "").strip(),
                "phone": phone,
                "amount": round(float(item.get("amount") or 0.0), 2),
            }
        )
        if phone:
            phones.append(phone)

    payment_amount = None
    payment_reference = order_doc.get("paystack_reference")
    if verify_data:
        try:
            payment_amount = round((float(verify_data.get("amount") or 0.0) / 100.0), 2)
        except Exception:
            payment_amount = None
        payment_reference = verify_data.get("reference") or payment_reference
    if payment_amount is None and session_doc:
        payment_amount = round(float(session_doc.get("paystack_total_expected") or 0.0), 2)
    if payment_amount is None:
        payment_amount = round(float(order_doc.get("charged_amount") or order_doc.get("total_amount") or 0.0), 2)

    return {
        "order_id": order_doc.get("order_id"),
        "payment_reference": payment_reference,
        "amount": payment_amount,
        "service_summary": service_summary,
        "phones": phones,
        "created_at": order_doc.get("created_at").isoformat() if order_doc.get("created_at") else "",
        "current_order_status": order_doc.get("status") or "pending",
        "receipt_url": f"/invoice/{order_doc.get('order_id')}" if order_doc.get("order_id") else "",
        "status_url": f"/check-status?phone={phones[0]}" if phones else "/check-status",
    }

def _json_safe_public_result(value: Any) -> Any:
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        safe = {}
        for k, v in value.items():
            if k == "order_doc":
                continue
            safe[k] = _json_safe_public_result(v)
        return safe
    if isinstance(value, list):
        return [_json_safe_public_result(v) for v in value]
    return value

def _deliver_public_results_checker(
    *, checker_type: str, phone: str, amount: float, order_id: str, line_index: int
) -> Tuple[bool, Dict[str, Any]]:
    checker_type = (checker_type or "").strip().lower()
    if checker_type not in {"wassce", "bece"}:
        return False, {"message": "Select a valid results checker type."}

    if not callable(_normalize_results_checker_phone) or not callable(_send_results_checker_sms):
        return False, {"message": "Results checker SMS delivery is not configured."}
    sms_phone = _normalize_results_checker_phone(phone)
    if not sms_phone:
        return False, {"message": "Enter a valid recipient phone number."}

    now = datetime.utcnow()
    sold_doc = wassce_col.find_one_and_update(
        {"type": checker_type, "status": "not_sold"},
        {"$set": {
            "status": "sold",
            "sold_to": "public_index_customer",
            "sold_at": now,
            "sold_phone": phone,
            "sms_phone": sms_phone,
            "order_id": order_id,
            "line_index": int(line_index),
            "sold_via": "index_page",
            "source": "index_page",
            "profit_applied": 0.0,
        }},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not sold_doc:
        return False, {"message": f"No {checker_type.upper()} results checker is currently available."}

    sms_text = str(sold_doc.get("message") or "").strip()
    sms_status = _send_results_checker_sms(sms_phone, sms_text)
    if sms_status != "sent":
        wassce_col.update_one(
            {"_id": sold_doc["_id"], "order_id": order_id},
            {
                "$set": {"status": "not_sold"},
                "$unset": {
                    "sold_to": "", "sold_at": "", "sold_phone": "", "sms_phone": "",
                    "order_id": "", "line_index": "", "sold_via": "", "source": "",
                    "profit_applied": "",
                },
            },
        )
        return False, {"message": "Could not send the results checker SMS right now. Please try again."}

    try:
        purchase_history_col.insert_one({
            "user_id": None,
            "checker_id": str(sold_doc["_id"]),
            "type": checker_type,
            "amount": round(float(amount or 0.0), 2),
            "profit": 0.0,
            "message": sms_text,
            "phone": phone,
            "order_id": order_id,
            "source": "index_page",
            "purchased_at": now,
        })
    except Exception:
        pass

    return True, {
        "checker_id": str(sold_doc["_id"]),
        "sms_status": sms_status,
        "type": checker_type.upper(),
    }

def _create_public_order_from_verified_payment(
    reference: str,
    verify_data: Dict[str, Any],
    server_cart: List[Dict[str, Any]],
    total_requested: float,
    defer_provider_processing: bool = False,
    order_status: str = "pending",
    payment_status: str = "paid",
    order_id_override: Optional[str] = None,
) -> Dict[str, Any]:
    order_id = (order_id_override or generate_order_id()).strip()
    results: List[Dict[str, Any]] = []
    debug_events: List[Dict[str, Any]] = []
    total_processing_amount = 0.0
    seen_keys = set()
    api_jobs: List[Dict[str, Any]] = []

    for idx, item in enumerate(server_cart, start=1):
        phone = (item.get("phone") or "").strip()
        service_id_raw = item.get("serviceId")
        svc_name = item.get("serviceName") or ""
        amt_total = _money(item.get("amount"))
        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

        if service_id_raw == RESULTS_CHECKER_SERVICE_ID:
            checker_type = _results_checker_type(value_obj, item.get("value"))
            ok, delivery = _deliver_public_results_checker(
                checker_type=checker_type,
                phone=phone,
                amount=amt_total,
                order_id=order_id,
                line_index=idx,
            )
            if not ok:
                raise ValueError(delivery.get("message") or "Results checker delivery failed.")
            total_processing_amount += amt_total
            results.append({
                "phone": phone,
                "base_amount": amt_total,
                "amount": amt_total,
                "profit_amount": 0.0,
                "profit_percent_used": 0.0,
                "value": checker_type.upper(),
                "value_obj": {"type": checker_type, "checker_type": checker_type},
                "serviceId": RESULTS_CHECKER_SERVICE_ID,
                "serviceName": RESULTS_CHECKER_NAME,
                "service_type": "MANUAL",
                "service_category": "results_checker",
                "provider": "arkesel_sms",
                "provider_reference": delivery.get("checker_id"),
                "checker_id": delivery.get("checker_id"),
                "line_status": "delivered",
                "api_status": "sent",
                "api_response": {"note": f"{checker_type.upper()} results checker sent by SMS."},
            })
            continue

        svc_doc = None
        svc_type = None
        if service_id_raw:
            try:
                svc_doc = services_col.find_one({
                    "_id": ObjectId(service_id_raw),
                    "display": {"$ne": "OFF"},
                    "public_display": {"$ne": "OFF"},
                })
                if svc_doc:
                    svc_type = svc_doc.get("type")
                    svc_name = svc_doc.get("name") or svc_name
            except Exception:
                svc_doc = None
                svc_type = None
            if not svc_doc:
                raise ValueError("This service is not available on the public page.")

        is_unavail, reason_text = _service_unavailability_reason(svc_doc)
        if is_unavail:
            raise ValueError(reason_text)

        network_id = _resolve_network_id(item, value_obj, svc_doc) if svc_doc else None
        bundle_key = _build_bundle_key(value_obj, item)
        amount_key = round(float(amt_total), 2)

        if phone and (network_id is not None) and (bundle_key is not None):
            cart_key = (phone, int(network_id), str(bundle_key), amount_key)
            if cart_key in seen_keys:
                results.append(
                    {
                        "phone": phone,
                        "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None,
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_in_cart",
                        "api_status": "skipped",
                        "api_response": {"note": "Duplicate line in this cart."},
                    }
                )
                continue
            seen_keys.add(cart_key)

        is_dup_strict = _has_processing_conflict_strict(
            phone, service_id_raw, svc_name, network_id, bundle_key, amount_key
        )
        if is_dup_strict:
            results.append(
                {
                    "phone": phone,
                    "base_amount": 0.0,
                    "amount": 0.0,
                    "originally_requested_amount": amt_total,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "skipped_duplicate_processing",
                    "api_status": "skipped",
                    "api_response": {"note": "Same line already processing; skipping."},
                }
            )
            continue

        resolved_network = _resolve_network_slug(svc_doc, item)
        svc_name_norm = (svc_name or "").strip().lower()
        is_mtn_express = svc_name_norm == "mtn express"
        is_mtn_normal = svc_name_norm == "mtn normal"

        svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
        api_allowed = svc_type_flag in ("ON", "API")
        if svc_type_flag == "OFF":
            api_allowed = False

        provider_raw = svc_doc.get("provider") if svc_doc else None
        svc_provider_from_db = str(provider_raw).strip().lower() if provider_raw is not None else ""
        provider_pref = (svc_provider_from_db or (item.get("provider") or "portal02"))
        svc_provider = str(provider_pref).strip().lower()
        if svc_provider not in ("portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"):
            jlog(
                "provider_defaulted_to_portal02",
                order_id=order_id,
                serviceId=service_id_raw,
                serviceName=svc_name,
                provider_raw=provider_raw,
                reason="missing_or_invalid",
            )
            svc_provider = "portal02"

        if svc_provider == "dataconnect" and not (is_mtn_express or is_mtn_normal):
            jlog(
                "provider_defaulted_to_portal02",
                order_id=order_id,
                serviceId=service_id_raw,
                serviceName=svc_name,
                provider_raw=provider_raw,
                reason="non_mtn_service_for_dataconnect",
            )
            svc_provider = "portal02"

        if svc_provider == "datakazina" and not (is_mtn_express or is_mtn_normal):
            jlog(
                "provider_defaulted_to_portal02",
                order_id=order_id,
                serviceId=service_id_raw,
                serviceName=svc_name,
                provider_raw=provider_raw,
                reason="non_mtn_service_for_datakazina",
            )
            svc_provider = "portal02"

        use_codecraft = api_allowed and svc_provider == "codecraft"
        use_datakazina = (
            (is_mtn_express or is_mtn_normal)
            and svc_provider == "datakazina"
            and api_allowed
        )
        use_dataconnect = (
            (is_mtn_express or is_mtn_normal)
            and svc_provider == "dataconnect"
            and api_allowed
        )
        use_skplug = api_allowed and svc_provider == "skplug"
        skplug_network = _resolve_skplug_network(svc_doc, item) if use_skplug else None
        use_bundleportal = api_allowed and svc_provider == "bundleportal"

        portal02_network_slug = None
        if api_allowed and svc_provider == "portal02":
            if resolved_network in ("mtn", "telecel", "airteltigo"):
                portal02_network_slug = resolved_network

        jlog(
            "checkout_line_routing",
            order_id=order_id,
            serviceName=svc_name,
            serviceId=service_id_raw,
            svc_provider_from_db=svc_provider_from_db,
            resolved_network=resolved_network,
            api_allowed=api_allowed,
            datakazina_selected=use_datakazina,
            selected_provider=(
                "codecraft" if use_codecraft else "bundleportal" if use_bundleportal else "datakazina" if use_datakazina else "dataconnect" if use_dataconnect else "skplug" if use_skplug else "portal02" if portal02_network_slug else "manual"
            ),
        )

        if use_codecraft:
            network = _resolve_codecraft_network(svc_doc, item)
            gig = _resolve_codecraft_gig(value_obj, item)
            package_map = _codecraft_get_packages_cached()
            provider_amount = package_map.get((network, gig)) if network and gig else None
            if not phone or not network or not gig or provider_amount is None:
                total_processing_amount += amt_total
                results.append({
                    "phone": phone, "base_amount": amt_total, "amount": amt_total,
                    "profit_amount": 0.0, "profit_percent_used": 0.0,
                    "value": item.get("value"), "value_obj": value_obj,
                    "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key, "line_status": "processing",
                    "api_status": "skipped_package_not_found" if network and gig else "skipped_missing_fields",
                    "api_response": {"note": "CodeCraft fields or package missing; queued for manual processing.", "network": network, "gig": gig},
                    "provider": "codecraft",
                })
                continue
            external_ref = f"{order_id}{idx}{uuid.uuid4().hex[:6]}"
            total_processing_amount += amt_total
            results.append({
                "phone": phone, "base_amount": amt_total, "amount": amt_total,
                "profit_amount": 0.0, "profit_percent_used": 0.0,
                "value": item.get("value"), "value_obj": value_obj,
                "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                "provider": "codecraft", "provider_network": network, "provider_gig": gig,
                "provider_package_amount": provider_amount, "provider_reference": None,
                "provider_order_id": None, "provider_request_order_id": external_ref,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key, "line_status": "processing", "api_status": "submitting",
                "api_response": {"note": "Submitting directly to CodeCraft"},
            })
            api_jobs.append({
                "provider_request_order_id": external_ref, "phone": phone, "amount": amt_total,
                "provider": "codecraft", "provider_network": network, "provider_gig": gig,
                "provider_amount": provider_amount,
                "service_id": svc_doc["_id"] if svc_doc else None, "line_index": idx,
            })
            continue

        if use_bundleportal:
            package_size_gb = _resolve_package_size_gb(value_obj, item)
            bp_network = resolved_network if resolved_network in ("mtn", "telecel", "airteltigo") else None
            if not phone or not bp_network or package_size_gb is None:
                total_processing_amount += amt_total
                results.append({
                    "phone": phone, "base_amount": amt_total, "amount": amt_total,
                    "profit_amount": 0.0, "profit_percent_used": 0.0,
                    "value": item.get("value"), "value_obj": value_obj,
                    "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key, "line_status": "processing",
                    "api_status": "skipped_missing_fields",
                    "api_response": {"note": "BundlePortal fields missing; queued for manual processing."},
                    "provider": "bundleportal",
                })
                continue
            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
            total_processing_amount += amt_total
            results.append({
                "phone": phone, "base_amount": amt_total, "amount": amt_total,
                "profit_amount": 0.0, "profit_percent_used": 0.0,
                "value": item.get("value"), "value_obj": value_obj,
                "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type,
                "provider": "bundleportal", "provider_network": bp_network,
                "provider_reference": None, "provider_order_id": None,
                "provider_request_order_id": external_ref, "package_size_gb": package_size_gb,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key, "line_status": "processing", "api_status": "submitting",
                "api_response": {"note": "Submitting directly to BundlePortal"},
            })
            api_jobs.append({
                "provider_request_order_id": external_ref, "phone": phone, "amount": amt_total,
                "provider": "bundleportal", "provider_network": bp_network,
                "package_size_gb": package_size_gb,
                "service_id": svc_doc["_id"] if svc_doc else None, "line_index": idx,
            })
            continue

        if use_datakazina:
            shared_bundle = _resolve_datakazina_shared_bundle(value_obj, item)
            dk_network_id = 3

            if not phone or not shared_bundle:
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": amt_total,
                        "amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": dk_network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {"note": "DataKazina fields missing; queued for processing."},
                        "provider": "datakazina",
                    }
                )
                continue

            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
            total_processing_amount += amt_total
            results.append(
                {
                    "phone": phone,
                    "base_amount": amt_total,
                    "amount": amt_total,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "datakazina",
                    "provider_network": "mtn",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": dk_network_id,
                    "shared_bundle": int(shared_bundle),
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to DataKazina"},
                }
            )
            api_jobs.append(
                {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "datakazina",
                    "network_id": dk_network_id,
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"] if svc_doc else None,
                    "line_index": idx,
                }
            )
            jlog(
                "datakazina_job_prepared",
                order_id=order_id,
                idx=idx,
                ref=external_ref,
                serviceId=service_id_raw,
                network_id=dk_network_id,
                shared_bundle=int(shared_bundle),
            )
            continue

        if use_dataconnect:
            shared_bundle = _resolve_shared_bundle_mb(value_obj, item)
            dc_network_id = _resolve_network_id(item, value_obj, svc_doc)
            if dc_network_id is None and resolved_network == "mtn":
                dc_network_id = 3
            try:
                dc_network_id = int(dc_network_id) if dc_network_id is not None else None
            except Exception:
                dc_network_id = None

            if not phone or not shared_bundle or not dc_network_id:
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": amt_total,
                        "amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {"note": "DataConnect fields missing; queued for processing."},
                        "provider": "dataconnect",
                    }
                )
                continue

            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
            total_processing_amount += amt_total
            results.append(
                {
                    "phone": phone,
                    "base_amount": amt_total,
                    "amount": amt_total,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "dataconnect",
                    "provider_network": "mtn",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": dc_network_id,
                    "shared_bundle": int(shared_bundle),
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to DataConnect"},
                }
            )
            api_jobs.append(
                {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "dataconnect",
                    "network_id": int(dc_network_id),
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"] if svc_doc else None,
                    "line_index": idx,
                }
            )
            continue

        if use_skplug:
            package_size_gb = _resolve_package_size_gb(value_obj, item)

            if not phone or not skplug_network or package_size_gb is None:
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": amt_total,
                        "amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {
                            "note": "SKPlug fields missing; queued for processing.",
                            "got": {
                                "phone": bool(phone),
                                "network": skplug_network,
                                "package_size_gb": package_size_gb,
                            },
                        },
                        "provider": "skplug",
                    }
                )
                continue

            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
            total_processing_amount += amt_total

            results.append(
                {
                    "phone": phone,
                    "base_amount": amt_total,
                    "amount": amt_total,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "skplug",
                    "provider_network": skplug_network,
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to SKPlug"},
                }
            )
            api_jobs.append(
                {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "skplug",
                    "provider_network": skplug_network,
                    "package_size_gb": package_size_gb,
                    "service_id": svc_doc["_id"] if svc_doc else None,
                    "line_index": idx,
                }
            )
            continue

        if not portal02_network_slug:
            total_processing_amount += amt_total
            results.append(
                {
                    "phone": phone,
                    "base_amount": amt_total,
                    "amount": amt_total,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "not_applicable_network",
                    "api_response": {"note": "No Portal-02 route for this line; queued for processing."},
                }
            )
            continue

        package_size_gb = _resolve_package_size_gb(value_obj, item)
        if not phone or package_size_gb is None:
            total_processing_amount += amt_total
            results.append(
                {
                    "phone": phone,
                    "base_amount": amt_total,
                    "amount": amt_total,
                    "profit_amount": 0.0,
                    "profit_percent_used": 0.0,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "skipped_missing_fields",
                    "api_response": {"note": "API fields missing; queued for processing."},
                }
            )
            continue

        external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
        total_processing_amount += amt_total
        results.append(
            {
                "phone": phone,
                "base_amount": amt_total,
                "amount": amt_total,
                "profit_amount": 0.0,
                "profit_percent_used": 0.0,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name,
                "service_type": svc_type,
                "provider": "portal02",
                "provider_network": portal02_network_slug,
                "provider_reference": None,
                "provider_order_id": None,
                "provider_request_order_id": external_ref,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key,
                "line_status": "processing",
                    "api_status": "submitting",
                "api_response": {"note": "Submitting directly to provider"},
            }
        )
        api_jobs.append(
            {
                "provider_request_order_id": external_ref,
                "phone": phone,
                "provider": "portal02",
                "portal02_network_slug": portal02_network_slug,
                "package_size_gb": package_size_gb,
                "service_id": svc_doc["_id"] if svc_doc else None,
                "raw_item": item,
            }
        )

    for it in (results or []):
        if not it.get("line_status"):
            it["line_status"] = "pending"
        if isinstance(it.get("value"), (dict, list)):
            it["value"] = ""
        if not it.get("value"):
            vo = it.get("value_obj") or {}
            vol = vo.get("volume")
            if isinstance(vol, (int, float)) and vol > 0:
                it["value"] = f"{(vol / 1000):g}GB"
            else:
                it["value"] = "N/A"

    if defer_provider_processing:
        for it in results:
            if it.get("line_status") == "processing":
                it["line_status"] = "awaiting_payment"
            if it.get("api_status") == "submitting":
                it["api_status"] = "payment_pending"
            note = it.get("api_response") if isinstance(it.get("api_response"), dict) else {}
            note["note"] = "Awaiting Paystack mobile money payment before provider processing."
            it["api_response"] = note

    if results and all(item.get("line_status") == "delivered" for item in results):
        order_status = "delivered"

    order_doc = {
        "user_id": None,
        "order_id": order_id,
        "items": results,
        "total_amount": total_requested,
        "charged_amount": round(total_processing_amount, 2),
        "profit_amount_total": 0.0,
        "status": order_status,
        "payment_status": payment_status,
        "payment_provider": "paystack" if payment_status == "pending" else None,
        "payment_channel": "mobile_money" if payment_status == "pending" else None,
        "channel": "arkesel_ussd" if defer_provider_processing else "public_web",
        "paid_from": "public_paystack",
        "paystack_reference": reference,
        "payment_reference": reference,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "debug": {"events": debug_events},
    }
    order_doc = {k: v for k, v in order_doc.items() if v is not None}
    if defer_provider_processing:
        order_doc["pending_provider_jobs"] = api_jobs
        order_doc["provider_processing_started"] = False

    try:
        created_docs, order_jobs = _persist_public_split_orders(
            base_order_fields=order_doc,
            results=results,
            api_jobs=api_jobs,
        )
    except DuplicateKeyError:
        existing = _find_public_order_by_reference(reference)
        if existing:
            return {
                "success": True,
                "status": "already_created",
                "order_id": existing.get("order_id"),
                "message": "Order already placed",
                "receipt": _build_public_receipt(existing, verify_data=verify_data),
                "order_doc": existing,
            }
        raise

    primary_order_doc = created_docs[0] if created_docs else order_doc
    primary_order_id = primary_order_doc.get("order_id") or order_id

    if order_jobs and not defer_provider_processing:
        try:
            for split_order_id, split_jobs in order_jobs:
                _background_process_providers(split_order_id, split_jobs)
            refreshed_docs = [orders_col.find_one({"order_id": doc.get("order_id")}) or doc for doc in created_docs]
            created_docs = refreshed_docs
            primary_order_doc = refreshed_docs[0] if refreshed_docs else primary_order_doc
        except Exception as e:
            jlog("public_checkout_provider_submit_error", order_id=primary_order_id, error=str(e))

    return {
        "success": True,
        "status": "completed",
        "order_id": primary_order_id,
        "order_ids": [doc.get("order_id") for doc in created_docs],
        "message": f"Order received and is processing. Order ID: {primary_order_id}",
        "receipt": _build_public_receipt(primary_order_doc, verify_data=verify_data),
        "order_doc": primary_order_doc,
    }

def finalize_paid_order(reference: str, payload_or_session_data: Optional[Dict[str, Any]] = None, source: str = "normal") -> Dict[str, Any]:
    reference = (reference or "").strip()
    if not reference:
        return {"success": False, "status": "failed", "message": "Payment reference is required."}

    session_doc = _load_payment_session(reference)
    incoming_cart = (payload_or_session_data or {}).get("cart") if isinstance(payload_or_session_data, dict) else None
    if isinstance(incoming_cart, list) and incoming_cart:
        try:
            session_doc = _upsert_payment_session(reference, incoming_cart, payload_or_session_data)
        except ValueError as exc:
            return {"success": False, "status": "failed", "message": str(exc)}

    existing_order = _find_public_order_by_reference(reference)
    if existing_order:
        payment_sessions_col.update_one(
            {"reference": reference},
            {
                "$set": {
                    "status": existing_order.get("status") or "processing",
                    "order_id": existing_order.get("order_id"),
                    "updated_at": datetime.utcnow(),
                }
            },
            upsert=True,
        )
        return {
            "success": True,
            "status": "already_created",
            "order_id": existing_order.get("order_id"),
            "message": "Order already placed",
            "receipt": _build_public_receipt(existing_order, session_doc=session_doc),
        }

    ok, verify_data, fail_reason = _verify_paystack(reference)
    if not ok:
        payment_sessions_col.update_one(
            {"reference": reference},
            {"$set": {"status": "verification_failed_recoverable", "last_error": fail_reason, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        return {
            "success": False,
            "status": "verification_failed_recoverable",
            "message": fail_reason or "Payment verification is pending.",
            "next_action": "retry_or_check_status",
        }

    paid_pes = int(verify_data.get("amount") or 0)
    paid_ghs = round(paid_pes / 100.0, 2)
    currency = (verify_data.get("currency") or "GHS").upper()
    if paid_pes <= 0 or currency != "GHS":
        return {"success": False, "status": "failed", "message": "Invalid payment amount/currency."}

    if not session_doc:
        return {
            "success": False,
            "status": "recoverable_failure",
            "message": "Payment was received, but the checkout recovery record is missing.",
            "next_action": "check_status",
        }

    total_expected = round(float(session_doc.get("total_expected") or 0.0), 2)
    paystack_total_expected = round(float(session_doc.get("paystack_total_expected") or 0.0), 2)
    if paystack_total_expected <= 0:
        return {"success": False, "status": "failed", "message": "Saved checkout amount is invalid."}
    if not _paid_enough(paid_pes, int(round(paystack_total_expected * 100))):
        return {"success": False, "status": "failed", "message": "Payment amount is less than required."}

    server_cart = session_doc.get("server_cart_snapshot") or []
    if not server_cart:
        raw_cart = session_doc.get("cart_snapshot") or []
        try:
            server_cart, total_expected = _reprice_public_cart(raw_cart)
        except ValueError as exc:
            return {"success": False, "status": "failed", "message": str(exc)}
        totals = _calc_public_paystack_totals(total_expected)
        payment_sessions_col.update_one(
            {"reference": reference},
            {
                "$set": {
                    "server_cart_snapshot": server_cart,
                    "total_expected": totals["base_total"],
                    "paystack_total_expected": totals["paystack_total"],
                    "fee_expected": totals["fee"],
                    "updated_at": datetime.utcnow(),
                }
            },
        )

    _ensure_public_transaction(reference, paid_ghs, total_expected, verify_data)
    payment_sessions_col.update_one(
        {"reference": reference},
        {
            "$set": {
                "status": "processing",
                "payment_verified": True,
                "verify_payload": {
                    "status": verify_data.get("status"),
                    "amount": verify_data.get("amount"),
                    "currency": verify_data.get("currency"),
                    "channel": verify_data.get("channel"),
                },
                "updated_at": datetime.utcnow(),
            }
        },
    )
    _append_processing_note(reference, f"Finalize attempt from {source}")

    try:
        created = _create_public_order_from_verified_payment(reference, verify_data, server_cart, total_expected)
    except ValueError as exc:
        payment_sessions_col.update_one(
            {"reference": reference},
            {"$set": {"status": "recoverable_failure", "last_error": str(exc), "updated_at": datetime.utcnow()}},
        )
        return {
            "success": False,
            "status": "recoverable_failure",
            "message": str(exc),
            "next_action": "retry_or_check_status",
        }
    except Exception as exc:
        payment_sessions_col.update_one(
            {"reference": reference},
            {"$set": {"status": "recoverable_failure", "last_error": str(exc), "updated_at": datetime.utcnow()}},
        )
        return {
            "success": False,
            "status": "recoverable_failure",
            "message": "Payment was received, but order processing did not finish. Do not pay again.",
            "next_action": "retry_or_check_status",
        }

    payment_sessions_col.update_one(
        {"reference": reference},
        {
            "$set": {
                "status": created.get("status") or "completed",
                "order_id": created.get("order_id"),
                "payment_verified": True,
                "updated_at": datetime.utcnow(),
                "finalized_at": datetime.utcnow(),
            }
        },
    )
    return created

def _public_status_by_reference(reference: str) -> Dict[str, Any]:
    reference = (reference or "").strip()
    if not reference:
        return {
            "payment_found": False,
            "payment_verified": False,
            "order_exists": False,
            "status": "failed",
            "message": "Payment reference is required.",
            "next_action": "none",
        }

    session_doc = _load_payment_session(reference)
    order_doc = _find_public_order_by_reference(reference)
    ok, verify_data, fail_reason = _verify_paystack(reference)

    if order_doc:
        order_status = (order_doc.get("status") or "").lower()
        return {
            "payment_found": True,
            "payment_verified": ok,
            "order_exists": True,
            "order_id": order_doc.get("order_id"),
            "status": "processing" if order_status in {"pending", "processing"} else "already_created",
            "message": "Order already placed",
            "next_action": "view_receipt",
            "receipt": _build_public_receipt(order_doc, verify_data=verify_data if ok else None, session_doc=session_doc),
        }

    if ok:
        return {
            "payment_found": True,
            "payment_verified": True,
            "order_exists": False,
            "order_id": (session_doc or {}).get("order_id"),
            "status": "pending",
            "message": "Payment found. You can safely reprocess this order.",
            "next_action": "reprocess",
        }

    return {
        "payment_found": bool(session_doc),
        "payment_verified": False,
        "order_exists": False,
        "order_id": (session_doc or {}).get("order_id"),
        "status": "pending" if session_doc else "recoverable_failure",
        "message": fail_reason or "Payment could not be confirmed yet.",
        "next_action": "retry_or_check_status",
    }

# ------------------ routes ------------------

@index_bp.route("/images/checker.png", methods=["GET"])
def results_checker_cover_image():
    return send_from_directory(os.path.join(os.path.dirname(__file__), "images"), "checker.png")

@index_bp.route("/", methods=["GET"])
def landing():
    """
    Simple public landing:
    - Loads services for display.
    - Public checkout is handled by /public-checkout.
    """
    # hansmart.store should not serve the landing page without a slug
    if _host_only(request.host) in _STORE_HOSTS:
        abort(404, description="Store not found")
    try:
        services, _ = load_services_for_landing()
    except Exception:
        services = []
    return render_template(
        "index.html",
        announcements=get_active_announcements("index_page"),
        services=services,
        paystack_pk=PAYSTACK_PUBLIC_KEY,
        enforce_known_number_check=_known_number_enforcement_enabled(),
    )

@index_bp.route("/public-payment-session", methods=["POST"])
def public_payment_session():
    data = request.get_json(silent=True) or {}
    cart = data.get("cart") or []
    reference = (data.get("reference") or "").strip()
    if not reference:
        return jsonify({"success": False, "message": "Payment reference is required"}), 400
    if not cart or not isinstance(cart, list):
        return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

    try:
        session_doc = _upsert_payment_session(reference, cart, data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    return jsonify(
        {
            "success": True,
            "status": "initialized",
            "reference": reference,
            "total_expected": session_doc.get("total_expected"),
            "paystack_total_expected": session_doc.get("paystack_total_expected"),
        }
    ), 200

@index_bp.route("/public-reprocess-order", methods=["POST"])
def public_reprocess_order():
    data = request.get_json(silent=True) or {}
    reference = (data.get("reference") or "").strip()
    result = finalize_paid_order(reference, payload_or_session_data=data, source="reprocess")
    status_code = 200 if result.get("success") or result.get("status") in {"verification_failed_recoverable", "recoverable_failure", "pending"} else 400
    return jsonify(_json_safe_public_result(result)), status_code

@index_bp.route("/public-order-status-by-ref", methods=["GET"])
def public_order_status_by_ref():
    reference = (request.args.get("reference") or "").strip()
    result = _public_status_by_reference(reference)
    return jsonify(_json_safe_public_result(result)), 200


@index_bp.route("/public-checkout", methods=["POST"])
def public_checkout():
    data = request.get_json(silent=True) or {}
    cart = data.get("cart") or []
    ps_info = data.get("paystack") or {}
    ps_ref = (ps_info.get("reference") or "").strip()

    if not cart or not isinstance(cart, list):
        return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400
    if not ps_ref:
        return jsonify({"success": False, "message": "Payment reference is required"}), 400
    try:
        _upsert_payment_session(ps_ref, cart, data)
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    result = finalize_paid_order(ps_ref, payload_or_session_data=data, source="normal")
    status_code = 200 if result.get("success") or result.get("status") in {"verification_failed_recoverable", "recoverable_failure", "pending"} else 400
    return jsonify(_json_safe_public_result(result)), status_code
