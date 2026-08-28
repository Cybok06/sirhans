# order_status.py  (FULL UPDATED VERSION — DataConnect MTN EXPRESS + MTN NORMAL ENABLED)
# ✅ DataConnect parsing stays as you already fixed
# ✅ Scheduler runs every 3 minutes
# ✅ DataConnect now checks BOTH: MTN EXPRESS + MTN NORMAL

from __future__ import annotations

import os
import json
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Blueprint, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

from db import db

order_status_bp = Blueprint("order_status", __name__)

# --- Collections ---
orders_col = db["orders"]

FINAL_STATUS = "delivered"

# ===== DataConnect Provider Config ==========================================
DATACONNECT_BASE_URL = "https://dataconnectgh.com/api/v1"
DATACONNECT_API_KEY = "d3ead3a6e67f483e2c18a6bbe5bbc1df9ab8984a"

# ===== CodeCraft Provider Config ============================================
CODECRAFT_BASE_URL = os.getenv("CODECRAFT_BASE_URL", "https://api.codecraftnetwork.com/api")
CODECRAFT_API_KEY = (os.getenv("CODECRAFT_API_KEY") or "260129025618-iafWYf-|FJJLo-ov1b8V-0?vzDK-AYNMWV").strip()

# ===== Portal-02 Provider Config =============================================
PORTAL02_BASE_URL = "https://www.portal-02.com/api/v1"
PORTAL02_API_KEY = "dk_yqFBqOoZJ3TET49kknXqmVQNabhefJlv"

# ===== BundlePortal Provider Config =========================================
BUNDLEPORTAL_BASE_URL = os.getenv("BUNDLEPORTAL_BASE_URL", "https://api.bundleportal.com/v1")
BUNDLEPORTAL_API_KEY = os.getenv("BUNDLEPORTAL_API_KEY", "").strip()
BUNDLEPORTAL_TIMEOUT = int(os.getenv("BUNDLEPORTAL_TIMEOUT", "30"))


# ===== Tiny JSON logger ======================================================
def jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")


def _log_status_blocked(order: Dict[str, Any], attempted_status: str, reason: str, source: str):
    jlog(
        "order_status_blocked",
        order_id=order.get("order_id"),
        mongo_id=str(order.get("_id")),
        attempted_status=attempted_status,
        current_status=(order.get("status") or ""),
        reason=reason,
        source=source,
    )


def _log_line_status_blocked(order: Dict[str, Any], item: Dict[str, Any], attempted_status: str, reason: str, source: str):
    jlog(
        "order_line_status_blocked",
        order_id=order.get("order_id"),
        mongo_id=str(order.get("_id")),
        provider=item.get("provider"),
        attempted_status=attempted_status,
        current_status=(item.get("line_status") or ""),
        reason=reason,
        source=source,
    )


def _normalize_status(s: str | None) -> str:
    val = (s or "").strip().lower()
    if val == "completed":
        return "delivered"
    return val


# ===== DataConnect order-status caller ======================================
def _fetch_dataconnect_order_status(transaction_id: str, order_id: str | None = None) -> Tuple[bool, Dict[str, Any]]:
    if not DATACONNECT_API_KEY:
        err = {"success": False, "message": "DATACONNECT API key not configured", "http_status": 500}
        jlog("dataconnect_status_config_error", order_id=order_id, transaction_id=transaction_id)
        return False, err

    url = f"{DATACONNECT_BASE_URL.rstrip('/')}/fetch-other-network-transaction"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": DATACONNECT_API_KEY,
    }
    payload = {"transaction_id": transaction_id}

    jlog("dataconnect_status_request", order_id=order_id, transaction_id=transaction_id, url=url)

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        text = resp.text or ""
        try:
            data = resp.json() if text.strip() else {}
        except Exception:
            data = {"raw": text} if text else {}

        if isinstance(data, dict):
            data.setdefault("http_status", resp.status_code)

        ok = False
        if resp.status_code == 200 and isinstance(data, dict):
            if data.get("transaction_code") or isinstance(data.get("order_items"), list) or data.get("id"):
                ok = True

        jlog("dataconnect_status_response", order_id=order_id, transaction_id=transaction_id, ok=ok, payload=data)
        return ok, data

    except requests.RequestException as e:
        jlog("dataconnect_status_network_error", order_id=order_id, transaction_id=transaction_id, error=str(e))
        return False, {"success": False, "message": str(e), "http_status": 599}


def _compute_order_status_from_items(items: List[Dict[str, Any]], current_status: str | None = None) -> str:
    if _normalize_status(current_status) == FINAL_STATUS:
        return FINAL_STATUS

    statuses = [_normalize_status(i.get("line_status")) for i in items]
    if not statuses:
        return "processing"

    if all(s == "delivered" for s in statuses):
        return "delivered"

    if all(s == "pending" for s in statuses):
        return "pending"

    if any(s in {"processing", "queued"} for s in statuses):
        return "processing"

    if all(s == "failed" for s in statuses):
        return "failed"

    return "processing"


def _map_dataconnect_status(status_raw: str) -> Tuple[str, str]:
    s = (status_raw or "").strip().lower()

    if s in {"success", "successful", "completed", "delivered", "delivered_successfully", "done", "complete"}:
        return "delivered", "success"

    if s in {"failed", "fail", "error", "reversed", "cancelled", "canceled"}:
        return "failed", "failed"

    if s in {"pending"}:
        return "pending", "pending"

    if s in {"processing", "queued", "initiated", "in_progress", "inprogress"}:
        return "processing", "processing"

    return "processing", "processing"


def _extract_dataconnect_status(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None

    order_items = payload.get("order_items")
    if isinstance(order_items, list) and order_items:
        first = order_items[0]
        if isinstance(first, dict) and first.get("status") is not None:
            return str(first.get("status"))

    for key in ("status", "transaction_status", "transactionStatus"):
        v = payload.get(key)
        if v is not None:
            return str(v)

    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("status", "transaction_status", "transactionStatus"):
            v = data.get(key)
            if v is not None:
                return str(v)

    tx = payload.get("transaction")
    if isinstance(tx, dict):
        for key in ("status", "transaction_status", "transactionStatus"):
            v = tx.get(key)
            if v is not None:
                return str(v)

    return None


def _apply_dataconnect_status_to_item(item: Dict[str, Any], status_raw: str, payload: Dict[str, Any], now: datetime, order: Optional[Dict[str, Any]] = None) -> None:
    line_status, api_status = _map_dataconnect_status(status_raw)

    current_line = _normalize_status(item.get("line_status"))
    if current_line == FINAL_STATUS and line_status != FINAL_STATUS:
        _log_line_status_blocked(order or {}, item, line_status, "final_line_status", "dataconnect_apply")
        line_status = FINAL_STATUS
        api_status = item.get("api_status") or "success"

    item["line_status"] = line_status
    item["api_status"] = api_status
    item["provider_status_last"] = status_raw
    item["provider_status_checked_at"] = now
    item["provider_status_payload"] = payload


# ✅ UPDATED: allow MTN EXPRESS + MTN NORMAL
def _is_dataconnect_supported_item(item: Dict[str, Any]) -> bool:
    name = (item.get("serviceName") or "").strip().lower()
    return name in {"mtn express", "mtn normal"}


def _fetch_codecraft_order_status(reference_id: str, mode: str, order_id: str | None = None) -> Tuple[bool, Dict[str, Any]]:
    if not CODECRAFT_API_KEY:
        return False, {"success": False, "message": "CODECRAFT API key not configured", "http_status": 500}
    mode = (mode or "regular").strip().lower()
    endpoint = "response_big_time.php" if mode == "bigtime" else "response_regular.php"
    url = f"{CODECRAFT_BASE_URL.rstrip('/')}/{endpoint}"
    headers = {"Accept": "application/json", "Content-Type": "application/json", "x-api-key": CODECRAFT_API_KEY}

    def parse_response(resp: requests.Response) -> Dict[str, Any]:
        try:
            payload = resp.json() if (resp.text or "").strip() else {}
        except Exception:
            payload = {"raw": resp.text or ""}
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)
        return payload

    def has_status(payload: Dict[str, Any]) -> bool:
        return isinstance(payload, dict) and isinstance(payload.get("data"), dict) and payload["data"].get("order_status") is not None

    try:
        resp = requests.request("GET", url, headers=headers, data=json.dumps({"reference_id": str(reference_id)}), timeout=30)
        payload = parse_response(resp)
        if not has_status(payload):
            resp = requests.post(url, headers=headers, json={"reference_id": str(reference_id)}, timeout=30)
            payload = parse_response(resp)
        ok = resp.status_code == 200 and bool(payload.get("success")) and has_status(payload)
        jlog("codecraft_status_response", order_id=order_id, reference_id=reference_id, mode=mode, ok=ok, payload=payload)
        return ok, payload
    except requests.RequestException as exc:
        jlog("codecraft_status_network_error", order_id=order_id, reference_id=reference_id, mode=mode, error=str(exc))
        return False, {"success": False, "message": str(exc), "http_status": 599}


def _extract_codecraft_status(payload: Dict[str, Any]) -> Optional[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    value = data.get("order_status") if isinstance(data, dict) else payload.get("order_status") if isinstance(payload, dict) else None
    return str(value) if value is not None else None


def _apply_codecraft_status_to_item(item: Dict[str, Any], status_raw: str, payload: Dict[str, Any], now: datetime, order: Optional[Dict[str, Any]] = None) -> None:
    status = (status_raw or "").strip().lower()
    if any(word in status for word in ("success", "completed", "delivered")):
        line_status, api_status = "delivered", "success"
    elif any(word in status for word in ("fail", "error", "reversed", "cancel")):
        line_status, api_status = "failed", "failed"
    elif "pending" in status:
        line_status, api_status = "pending", "pending"
    else:
        line_status, api_status = "processing", "processing"
    if _normalize_status(item.get("line_status")) == FINAL_STATUS and line_status != FINAL_STATUS:
        _log_line_status_blocked(order or {}, item, line_status, "final_line_status", "codecraft_apply")
        line_status, api_status = FINAL_STATUS, item.get("api_status") or "success"
    item.update({"line_status": line_status, "api_status": api_status, "provider_status_last": status_raw,
                 "provider_status_checked_at": now, "provider_status_payload": payload})


def _fetch_bundleportal_order_status(order_reference: str, order_id: str | None = None) -> Tuple[bool, Dict[str, Any]]:
    if not BUNDLEPORTAL_API_KEY:
        err = {"success": False, "message": "BUNDLEPORTAL API key not configured", "http_status": 500}
        jlog("bundleportal_status_config_error", order_id=order_id, order_reference=order_reference)
        return False, err

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": BUNDLEPORTAL_API_KEY,
    }
    body = {"action": "check_status", "order_reference": str(order_reference)}
    jlog("bundleportal_status_request", order_id=order_id, order_reference=order_reference)

    try:
        resp = requests.post(
            BUNDLEPORTAL_BASE_URL.rstrip("/"),
            headers=headers,
            json=body,
            timeout=BUNDLEPORTAL_TIMEOUT,
        )
        text = resp.text or ""
        try:
            payload = resp.json() if text.strip() else {}
        except Exception:
            payload = {"raw": text} if text else {}
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)
        ok = (
            200 <= resp.status_code < 300
            and isinstance(payload, dict)
            and payload.get("success") is True
            and isinstance(payload.get("data"), dict)
        )
        jlog(
            "bundleportal_status_response",
            order_id=order_id,
            order_reference=order_reference,
            ok=ok,
            payload=payload,
        )
        return ok, payload
    except requests.RequestException as exc:
        jlog("bundleportal_status_network_error", order_id=order_id, order_reference=order_reference, error=str(exc))
        return False, {"success": False, "message": str(exc), "http_status": 599}


def _extract_bundleportal_status(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict) and data.get("status") is not None:
        return str(data.get("status"))
    return None


def _map_bundleportal_status(status_raw: str) -> Tuple[str, str]:
    status = (status_raw or "").strip().lower()
    if status == "completed":
        return "delivered", "success"
    if status == "failed":
        return "failed", "failed"
    if status in {"processing", "cached"}:
        return "processing", status
    return "processing", "processing"


def _apply_bundleportal_status_to_item(
    item: Dict[str, Any],
    status_raw: str,
    payload: Dict[str, Any],
    now: datetime,
    order: Optional[Dict[str, Any]] = None,
) -> None:
    line_status, api_status = _map_bundleportal_status(status_raw)
    current_line = _normalize_status(item.get("line_status"))
    if current_line == FINAL_STATUS and line_status != FINAL_STATUS:
        _log_line_status_blocked(order or {}, item, line_status, "final_line_status", "bundleportal_apply")
        line_status = FINAL_STATUS
        api_status = item.get("api_status") or "success"

    data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else {}
    item["line_status"] = line_status
    item["api_status"] = api_status
    item["provider_status"] = (status_raw or "").strip().lower()
    item["provider_status_last"] = status_raw
    item["provider_status_checked_at"] = now
    item["provider_status_payload"] = payload
    if data.get("reference"):
        item["provider_reference"] = data.get("reference")
    if data.get("order_id"):
        item["provider_order_id"] = data.get("order_id")
    if data.get("failure_reason") is not None:
        item["provider_failure_reason"] = data.get("failure_reason")


def _fetch_portal02_order_status(order_ref: str, order_id: str | None = None) -> Tuple[bool, Dict[str, Any]]:
    if not PORTAL02_API_KEY:
        err = {"success": False, "message": "PORTAL02 API key not configured", "http_status": 500}
        jlog("portal02_status_config_error", order_id=order_id, order_ref=order_ref)
        return False, err

    url = f"{PORTAL02_BASE_URL.rstrip('/')}/order/status/{order_ref}"
    headers = {"Accept": "application/json", "x-api-key": PORTAL02_API_KEY}

    jlog("portal02_status_request", order_id=order_id, order_ref=order_ref, url=url)

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        text = resp.text or ""
        try:
            data = resp.json() if text.strip() else {}
        except Exception:
            data = {"raw": text} if text else {}

        if isinstance(data, dict):
            data.setdefault("http_status", resp.status_code)

        ok = resp.status_code == 200 and isinstance(data, dict) and bool(data.get("success")) is True and isinstance(data.get("order"), dict)
        jlog("portal02_status_response", order_id=order_id, order_ref=order_ref, ok=ok, payload=data)
        return ok, data
    except requests.RequestException as e:
        jlog("portal02_status_network_error", order_id=order_id, order_ref=order_ref, error=str(e))
        return False, {"success": False, "message": str(e), "http_status": 599}


def _extract_portal02_status(payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    order = payload.get("order")
    if isinstance(order, dict) and order.get("status") is not None:
        return str(order.get("status"))
    return None


def _map_portal02_status(status_raw: str) -> Tuple[str, str]:
    s = (status_raw or "").strip().lower()

    if s in {"delivered", "resolved"}:
        return "delivered", "success"
    if s in {"failed", "cancelled", "canceled"}:
        return "failed", "failed"
    if s == "refunded":
        return "failed", "refunded"
    if s == "pending":
        return "pending", "pending"
    if s == "processing":
        return "processing", "processing"

    return "processing", "processing"


def _apply_portal02_status_to_item(item: Dict[str, Any], status_raw: str, payload: Dict[str, Any], now: datetime, order: Optional[Dict[str, Any]] = None) -> None:
    line_status, api_status = _map_portal02_status(status_raw)

    current_line = _normalize_status(item.get("line_status"))
    if current_line == FINAL_STATUS and line_status != FINAL_STATUS:
        _log_line_status_blocked(order or {}, item, line_status, "final_line_status", "portal02_apply")
        line_status = FINAL_STATUS
        api_status = item.get("api_status") or "success"

    item["line_status"] = line_status
    item["api_status"] = api_status
    item["provider_status_last"] = status_raw
    item["provider_status_checked_at"] = now
    item["provider_status_payload"] = payload


# ===== Core sync logic =======================================================
def _run_order_status_sync() -> Dict[str, Any]:
    now = datetime.utcnow()

    checked_orders = 0
    dataconnect_checked_orders = 0
    codecraft_checked_orders = 0
    bundleportal_checked_orders = 0
    updated_orders = 0
    updated_lines = 0
    completed_lines = 0
    failed_lines = 0
    still_processing_lines = 0
    skipped_missing_reference_id = 0

    # ✅ UPDATED: include MTN NORMAL in DataConnect cursor match too
    cursor = (
        orders_col.find(
            {
                "$or": [
                    {"status": {"$in": ["pending", "processing"]}},
                    {
                        "items": {
                            "$elemMatch": {
                                "provider": "dataconnect",
                                "line_status": {"$in": ["pending", "processing", "queued"]},
                                "serviceName": {"$regex": r"^\s*mtn\s+(express|normal)\s*$", "$options": "i"},
                            }
                        }
                    },
                    {
                        "items": {
                            "$elemMatch": {
                                "provider": "portal02",
                                "line_status": {"$in": ["pending", "processing", "queued"]},
                            }
                        }
                    },
                    {
                        "items": {
                            "$elemMatch": {
                                "provider": "codecraft",
                                "line_status": {"$in": ["pending", "processing", "queued"]},
                            }
                        }
                    },
                    {
                        "items": {
                            "$elemMatch": {
                                "provider": "bundleportal",
                                "line_status": {"$in": ["pending", "processing", "queued"]},
                            }
                        }
                    },
                ]
            }
        )
        .sort("created_at", -1)
        .limit(50)
    )

    for order in cursor:
        checked_orders += 1

        oid = order.get("_id")
        order_id = order.get("order_id")
        current_status = _normalize_status(order.get("status"))
        source = order.get("source") if isinstance(order.get("source"), dict) else {}
        is_unpaid_ussd = (
            (order.get("channel") == "arkesel_ussd" or source.get("provider") == "arkesel")
            and str(order.get("payment_status") or "").strip().lower() != "paid"
        )
        if is_unpaid_ussd:
            _log_status_blocked(order, "sync_update", "unpaid_ussd_payment_gate", "status_sync")
            continue

        if current_status == FINAL_STATUS:
            _log_status_blocked(order, "sync_update", "final_status", "status_sync")
            continue

        items = order.get("items", []) or []
        changed = False

        for item in items:
            if _normalize_status(item.get("line_status")) not in {"pending", "processing", "queued"}:
                continue

            provider = item.get("provider")

            # --- DataConnect (NOW checks MTN EXPRESS + MTN NORMAL) ---
            if provider == "dataconnect":
                if not _is_dataconnect_supported_item(item):
                    continue

                dataconnect_checked_orders += 1

                transaction_id = item.get("provider_reference") or item.get("provider_order_id") or item.get("provider_request_order_id")
                if not transaction_id:
                    skipped_missing_reference_id += 1
                    item["provider_status_checked_at"] = now
                    still_processing_lines += 1
                    changed = True
                    continue

                ok, payload = _fetch_dataconnect_order_status(transaction_id, order_id)
                status_raw = _extract_dataconnect_status(payload)

                if status_raw is None:
                    item["provider_status_checked_at"] = now
                    item["provider_status_payload"] = payload
                    still_processing_lines += 1
                    changed = True
                    continue

                _apply_dataconnect_status_to_item(item, status_raw, payload, now, order=order)
                changed = True
                updated_lines += 1

                ls = _normalize_status(item.get("line_status"))
                if ls == "delivered":
                    completed_lines += 1
                elif ls == "failed":
                    failed_lines += 1
                else:
                    still_processing_lines += 1

                jlog(
                    "dataconnect_line_checked",
                    order_id=order_id,
                    mongo_id=str(oid),
                    transaction_id=transaction_id,
                    provider_ok=ok,
                    status_raw=status_raw,
                    mapped_line_status=item.get("line_status"),
                    serviceName=item.get("serviceName"),
                )
                continue

            # --- CodeCraft ---
            if provider == "codecraft":
                codecraft_checked_orders += 1
                reference_id = item.get("provider_reference") or item.get("provider_order_id") or item.get("provider_request_order_id")
                if not reference_id:
                    skipped_missing_reference_id += 1
                    item["provider_status_checked_at"] = now
                    still_processing_lines += 1
                    changed = True
                    continue
                mode = (item.get("provider_mode") or item.get("codecraft_mode") or "regular").strip().lower()
                ok, payload = _fetch_codecraft_order_status(str(reference_id), mode, order_id)
                status_raw = _extract_codecraft_status(payload)
                if status_raw is None:
                    item["provider_status_checked_at"] = now
                    item["provider_status_payload"] = payload
                    still_processing_lines += 1
                    changed = True
                    continue
                _apply_codecraft_status_to_item(item, status_raw, payload, now, order=order)
                changed = True
                updated_lines += 1
                line_status = _normalize_status(item.get("line_status"))
                if line_status == "delivered": completed_lines += 1
                elif line_status == "failed": failed_lines += 1
                else: still_processing_lines += 1
                jlog("codecraft_line_checked", order_id=order_id, mongo_id=str(oid), reference_id=reference_id,
                     provider_ok=ok, status_raw=status_raw, mapped_line_status=item.get("line_status"))
                continue

            # --- BundlePortal ---
            if provider == "bundleportal":
                bundleportal_checked_orders += 1
                # BundlePortal's check_status action expects our idempotency
                # order ID, not the provider's KT-* reference.
                order_reference = item.get("provider_order_id") or item.get("provider_request_order_id")
                if not order_reference:
                    skipped_missing_reference_id += 1
                    item["provider_status_checked_at"] = now
                    still_processing_lines += 1
                    changed = True
                    continue

                ok, payload = _fetch_bundleportal_order_status(str(order_reference), order_id)
                status_raw = _extract_bundleportal_status(payload) if ok else None
                if status_raw is None:
                    item["provider_status_checked_at"] = now
                    item["provider_status_payload"] = payload
                    still_processing_lines += 1
                    changed = True
                    continue

                _apply_bundleportal_status_to_item(item, status_raw, payload, now, order=order)
                changed = True
                updated_lines += 1
                line_status = _normalize_status(item.get("line_status"))
                if line_status == "delivered":
                    completed_lines += 1
                elif line_status == "failed":
                    failed_lines += 1
                else:
                    still_processing_lines += 1

                jlog(
                    "bundleportal_line_checked",
                    order_id=order_id,
                    mongo_id=str(oid),
                    order_reference=order_reference,
                    provider_ok=ok,
                    status_raw=status_raw,
                    mapped_line_status=item.get("line_status"),
                )
                continue

            # --- Portal02 (unchanged) ---
            if provider == "portal02":
                portal_ref = item.get("provider_order_id") or item.get("provider_reference") or item.get("provider_request_order_id")
                if not portal_ref:
                    skipped_missing_reference_id += 1
                    item["provider_status_checked_at"] = now
                    still_processing_lines += 1
                    changed = True
                    continue

                ok, payload = _fetch_portal02_order_status(portal_ref, order_id)
                status_raw = _extract_portal02_status(payload)

                if status_raw is None:
                    item["provider_status_checked_at"] = now
                    item["provider_status_payload"] = payload
                    still_processing_lines += 1
                    changed = True
                    continue

                _apply_portal02_status_to_item(item, status_raw, payload, now, order=order)
                changed = True
                updated_lines += 1

                ls = _normalize_status(item.get("line_status"))
                if ls == "delivered":
                    completed_lines += 1
                elif ls == "failed":
                    failed_lines += 1
                else:
                    still_processing_lines += 1

                jlog(
                    "portal02_line_checked",
                    order_id=order_id,
                    mongo_id=str(oid),
                    portal_ref=portal_ref,
                    provider_ok=ok,
                    status_raw=status_raw,
                    mapped_line_status=item.get("line_status"),
                )
                continue

        if not changed:
            continue

        new_order_status = _compute_order_status_from_items(items, current_status=current_status)

        update_filter: Dict[str, Any] = {"_id": oid}
        if new_order_status != FINAL_STATUS:
            update_filter["status"] = {"$ne": FINAL_STATUS}

        res = orders_col.update_one(update_filter, {"$set": {"items": items, "status": new_order_status, "updated_at": now}})

        if res.modified_count:
            updated_orders += 1
        elif new_order_status != FINAL_STATUS:
            _log_status_blocked(order, new_order_status, "db_guard", "status_sync")

        jlog("order_status_sync_updated_order", order_id=order_id, mongo_id=str(oid), new_status=new_order_status)

    summary = {
        "checked_orders": checked_orders,
        "dataconnect_checked_orders": dataconnect_checked_orders,
        "codecraft_checked_orders": codecraft_checked_orders,
        "bundleportal_checked_orders": bundleportal_checked_orders,
        "updated_orders": updated_orders,
        "updated_lines": updated_lines,
        "completed_lines": completed_lines,
        "failed_lines": failed_lines,
        "still_processing_lines": still_processing_lines,
        "skipped_missing_reference_id": skipped_missing_reference_id,
        "timestamp": now.isoformat() + "Z",
        "interval_minutes": 3,
    }

    jlog("order_status_sync_summary", **summary)
    return summary


# ===== Route: manual sync ====================================================
@order_status_bp.route("/order-status-sync", methods=["GET"])
def sync_order_status():
    try:
        summary = _run_order_status_sync()
        return jsonify({"success": True, "summary": summary}), 200
    except Exception:
        jlog("order_status_sync_uncaught", error=traceback.format_exc())
        return jsonify({"success": False, "message": "Server error"}), 500


# ===== Portal-02 Webhook Receiver (unchanged) ===============================
@order_status_bp.route("/webhooks/portal02/orders", methods=["POST"])
def portal02_webhook():
    now = datetime.utcnow()
    payload = request.get_json(silent=True) or {}

    order_id_p = payload.get("orderId")
    ref_p = payload.get("reference")
    status_raw = payload.get("status")
    recipient = payload.get("recipient")

    jlog("portal02_webhook_in", orderId=order_id_p, reference=ref_p, status=status_raw, recipient=recipient)

    if not status_raw or (not order_id_p and not ref_p):
        return jsonify({"success": True, "ignored": True}), 200

    q = {
        "items": {
            "$elemMatch": {
                "provider": "portal02",
                "$or": [{"provider_order_id": order_id_p}, {"provider_reference": ref_p}],
            }
        }
    }

    order = orders_col.find_one(q)
    if not order:
        jlog("portal02_webhook_no_match", orderId=order_id_p, reference=ref_p)
        return jsonify({"success": True, "matched": False}), 200

    items = order.get("items", []) or []
    changed = False

    for item in items:
        if item.get("provider") != "portal02":
            continue
        if order_id_p and item.get("provider_order_id") == order_id_p:
            _apply_portal02_status_to_item(item, str(status_raw), payload, now, order=order)
            changed = True
        elif ref_p and item.get("provider_reference") == ref_p:
            _apply_portal02_status_to_item(item, str(status_raw), payload, now, order=order)
            changed = True

    if changed:
        new_order_status = _compute_order_status_from_items(items, current_status=(order.get("status") or "").lower())
        orders_col.update_one(
            {"_id": order["_id"], "status": {"$ne": FINAL_STATUS}},
            {"$set": {"items": items, "status": new_order_status, "updated_at": now}},
        )
        jlog("portal02_webhook_applied", order_id=order.get("order_id"), new_status=new_order_status)

    return jsonify({"success": True}), 200


# ===== Background scheduler: run every 3 minutes ============================
def _scheduled_sync_job():
    try:
        jlog("order_status_scheduled_run_start")
        summary = _run_order_status_sync()
        jlog("order_status_scheduled_run_done", **summary)
    except Exception:
        jlog("order_status_scheduled_run_error", error=traceback.format_exc())


def _scheduled_ussd_payment_reconciliation():
    try:
        from paystack_webhook import reconcile_arkesel_pending_payments

        jlog("ussd_payment_reconciliation_start")
        summary = reconcile_arkesel_pending_payments(limit=50)
        jlog("ussd_payment_reconciliation_done", **summary)
    except Exception:
        jlog("ussd_payment_reconciliation_error", error=traceback.format_exc())


scheduler = BackgroundScheduler(timezone="UTC")

scheduler.add_job(
    _scheduled_sync_job,
    "interval",
    minutes=3,
    max_instances=1,
    coalesce=True,
    id="order_status_sync",
)

scheduler.add_job(
    _scheduled_ussd_payment_reconciliation,
    "interval",
    minutes=max(int(os.getenv("USSD_PAYMENT_RECONCILE_MINUTES", "3")), 1),
    max_instances=1,
    coalesce=True,
    id="ussd_payment_reconciliation",
)

try:
    scheduler.start()
    jlog("order_status_scheduler_started", interval_minutes=3)
except Exception:
    jlog("order_status_scheduler_start_failed", error=traceback.format_exc())
