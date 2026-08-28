# routes/store_page.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import os, json, re, ast, traceback, uuid
from urllib.parse import urljoin, quote

import requests
from bson import ObjectId
from pymongo import ReturnDocument
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_file,
    abort,
)

from announcement_utils import get_active_announcements
from db import db
import gridfs
from deposit import PAYSTACK_PUBLIC_KEY as DEPOSIT_PAYSTACK_PK
from deposit import PAYSTACK_SECRET_KEY as DEPOSIT_PAYSTACK_SK


# ---------------------------------------------------------------------
# Collections
# ---------------------------------------------------------------------
services_col = db["services"]
stores_col = db["stores"]
balances_col = db["balances"]
orders_col = db["orders"]
transactions_col = db["transactions"]
users_col = db["users"]
store_accounts_col = db["store_accounts"]
wassce_col = db["wassce_checker"]
purchase_history_col = db["purchase_history"]
complaints_col = db["complaints"]

# ✅ PRIMARY: Store products collection used by /api/store-products/*
store_products_col = db["store_products"]

# ✅ Legacy products collection (optional fallback)
products_col = db.get_collection("products")

# --- GridFS bucket ---
fs = gridfs.GridFS(db)

stores_bp = Blueprint("stores", __name__)


@stores_bp.route("/api/store/<slug>/reports", methods=["POST"])
def submit_store_service_report(slug: str):
    """Send a public storefront service report to the store owner's inbox."""
    store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
    if not store_doc:
        return jsonify({"success": False, "message": "Store not found."}), 404

    data = request.get_json(silent=True) or {}
    service_name = str(data.get("service") or "").strip()[:120]
    offer = str(data.get("offer") or "").strip()[:80]
    phone = re.sub(r"\D+", "", str(data.get("phone") or ""))[:10]
    order_date_raw = str(data.get("order_date") or "").strip()
    order_time_raw = str(data.get("order_time") or "").strip()

    if not service_name or not offer or not re.fullmatch(r"0\d{9}", phone):
        return jsonify({"success": False, "message": "Please provide valid service, bundle and phone details."}), 400

    try:
        order_date = datetime.strptime(f"{order_date_raw} {order_time_raw}", "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Please provide a valid order date and time."}), 400

    owner_id = store_doc.get("owner_id")
    if not owner_id:
        return jsonify({"success": False, "message": "This store cannot receive reports right now."}), 400

    duplicate_since = datetime.utcnow() - timedelta(minutes=5)
    duplicate = complaints_col.find_one(
        {
            "complaint_type": "store_report",
            "store_slug": slug,
            "service_name": service_name,
            "reporter_phone": phone,
            "submitted_at": {"$gte": duplicate_since},
        },
        {"_id": 1},
    )
    if duplicate:
        return jsonify({"success": True, "duplicate": True, "message": "Your report has already been received."}), 200

    now = datetime.utcnow()
    doc = {
        "user_id": owner_id,
        "complaint_type": "store_report",
        "source": "store_page",
        "store_slug": slug,
        "store_name": store_doc.get("name") or slug,
        "service_name": service_name,
        "offer": offer,
        "reporter_phone": phone,
        "whatsapp": phone,
        "order_date": order_date,
        "order_number_provided": str(data.get("order_id") or "").strip()[:80],
        "description": str(data.get("description") or "Customer reported a service delivery issue through the store.").strip()[:1000],
        "submitted_at": now,
        "updated_at": now,
        "status": "pending",
        "forwarded_to_admin": False,
        "admin_visibility": False,
    }
    result = complaints_col.insert_one(doc)
    return jsonify({"success": True, "report_id": str(result.inserted_id), "message": "Your report has been sent to the store support team."}), 201


# ---------------------------------------------------------------------
# Import helpers from checkout.py (keep compatibility)
# ---------------------------------------------------------------------
_checkout_helpers: Dict[str, Any] = {}
try:
    from checkout import (  # type: ignore
        _effective_profit_percent,
        _derive_base_profit,
        _coerce_value_obj,
        _to_float,
        _money,
        generate_order_id,
        _service_unavailability_reason,
        _resolve_network_slug,
        _resolve_codecraft_network,
        _resolve_codecraft_gig,
        _codecraft_get_packages_cached,
        _resolve_package_size_gb,
        _resolve_skplug_network,
        _resolve_shared_bundle_mb,
        _resolve_datakazina_shared_bundle,
        _background_process_providers,
        _known_number_enforcement_enabled,
        _known_number_validation_error,
        _service_requires_known_number_verification,
        jlog,
    )
    try:
        from checkout import _insert_transaction_doc_like_checkout  # type: ignore
        _checkout_helpers["txn_fn"] = _insert_transaction_doc_like_checkout
    except Exception:
        pass
    try:
        from checkout import _insert_order_doc_like_checkout  # type: ignore
        _checkout_helpers["order_fn"] = _insert_order_doc_like_checkout
    except Exception:
        pass
except Exception:  # pragma: no cover
    from .checkout import (  # type: ignore
        _effective_profit_percent,
        _derive_base_profit,
        _coerce_value_obj,
        _to_float,
        _money,
        generate_order_id,
        _service_unavailability_reason,
        _resolve_network_slug,
        _resolve_codecraft_network,
        _resolve_codecraft_gig,
        _codecraft_get_packages_cached,
        _resolve_package_size_gb,
        _resolve_skplug_network,
        _resolve_shared_bundle_mb,
        _resolve_datakazina_shared_bundle,
        _background_process_providers,
        _known_number_enforcement_enabled,
        _known_number_validation_error,
        _service_requires_known_number_verification,
        jlog,
    )
    try:
        from .checkout import _insert_transaction_doc_like_checkout  # type: ignore
        _checkout_helpers["txn_fn"] = _insert_transaction_doc_like_checkout
    except Exception:
        pass
    try:
        from .checkout import _insert_order_doc_like_checkout  # type: ignore
        _checkout_helpers["order_fn"] = _insert_order_doc_like_checkout
    except Exception:
        pass


# ---------------------------------------------------------------------
# Config (ENV)
# ---------------------------------------------------------------------
def _clean_key(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ""

def _is_pk(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("pk_")

def _is_sk(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("sk_")

_raw_pk = _clean_key(DEPOSIT_PAYSTACK_PK) or _clean_key(os.getenv("PAYSTACK_PUBLIC_KEY", "")) or _clean_key(os.getenv("PAYSTACK_PK", ""))
_raw_sk = _clean_key(DEPOSIT_PAYSTACK_SK) or _clean_key(os.getenv("PAYSTACK_SECRET_KEY", "")) or _clean_key(os.getenv("PAYSTACK_SK", ""))

# Match index.py source of truth: deposit keys (fallback to env)
PAYSTACK_PUBLIC_KEY: str = _raw_pk
PAYSTACK_SECRET_KEY: str = _raw_sk

# auto-fix swap if misconfigured
if _is_sk(PAYSTACK_PUBLIC_KEY) and _is_pk(PAYSTACK_SECRET_KEY):
    PAYSTACK_PUBLIC_KEY, PAYSTACK_SECRET_KEY = PAYSTACK_SECRET_KEY, PAYSTACK_PUBLIC_KEY

# defensive recovery
if not _is_pk(PAYSTACK_PUBLIC_KEY) and _is_pk(PAYSTACK_SECRET_KEY):
    PAYSTACK_PUBLIC_KEY = PAYSTACK_SECRET_KEY
if not _is_sk(PAYSTACK_SECRET_KEY) and _is_sk(PAYSTACK_PUBLIC_KEY):
    PAYSTACK_SECRET_KEY = PAYSTACK_PUBLIC_KEY

TARGET_STORE_HOST: str = os.getenv("STORE_PUBLIC_HOST", "www.hansmart.store")
STORE_PATH_PREFIXES: Tuple[str, ...] = ("/s/",)
DEFAULT_STORE_SHARE_IMAGE = "https://imagedelivery.net/h9fmMoa1o2c2P55TcWJGOg/29d9af76-b72e-4070-954e-184224478100/public"
RESULTS_CHECKER_SERVICE_ID = "results_checker_service"
RESULTS_CHECKER_IMAGE_URL = "https://resultschecker.com.gh/site-assets/images/bece-waec-wassce-results-checker-ghana.jpeg"
RESULTS_CHECKER_NAME = "Results Checker"

try:
    from admin_balance import _send_sms as _send_arkesel_sms  # type: ignore
    from admin_balance import _normalize_phone as _normalize_arkesel_phone  # type: ignore
except Exception:  # pragma: no cover
    _ARKESEL_API_KEY = os.getenv("ARKESEL_API_KEY", "")
    _ARKESEL_SENDER_ID = os.getenv("ARKESEL_SENDER_ID", "Sir Hans")

    def _normalize_arkesel_phone(raw: str) -> Optional[str]:
        if not raw:
            return None
        p = str(raw).strip().replace(" ", "").replace("-", "").replace("+", "")
        if p.startswith("0") and len(p) == 10:
            p = "233" + p[1:]
        if p.startswith("233") and len(p) == 12:
            return p
        return None

    def _send_arkesel_sms(msisdn: str, message: str) -> str:
        try:
            url = (
                "https://sms.arkesel.com/sms/api?action=send-sms"
                f"&api_key={_ARKESEL_API_KEY}"
                f"&to={msisdn}"
                f"&from={quote(_ARKESEL_SENDER_ID)}"
                f"&sms={quote(message)}"
            )
            resp = requests.get(url, timeout=12)
            if resp.status_code == 200 and '"code":"ok"' in resp.text:
                return "sent"
            return "failed"
        except Exception:
            return "error"

# Canonical store host enforcement (public store + store tools)
_CANONICAL_STORE_HOST = TARGET_STORE_HOST.strip().lower()
_DEV_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}

def _host_only(v: str) -> str:
    return (v or "").split(":", 1)[0].strip().lower()

def _store_host_redirect():
    host = _host_only(request.host)
    if not host or host in _DEV_HOSTS or host.endswith(".local"):
        return None
    if host != _host_only(_CANONICAL_STORE_HOST):
        q = request.query_string.decode("utf-8")
        url = f"https://{_CANONICAL_STORE_HOST}{request.path}" + (f"?{q}" if q else "")
        return redirect(url, code=301)
    return None

def _single_order_status_from_line(item: Dict[str, Any], fallback: str = "pending") -> str:
    line_status = str((item or {}).get("line_status") or "").strip().lower()
    if line_status in {"skipped_duplicate_processing", "skipped_duplicate_in_cart"}:
        return "skipped"
    if line_status == "delivered":
        return "delivered"
    if line_status == "failed":
        return "failed"
    return fallback


def _persist_store_split_orders(
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
        total_amount = round(float(item.get("amount") or 0.0), 2)
        profit_amount_total = round(float(item.get("profit_amount") or 0.0), 2)
        status = _single_order_status_from_line(item, str(base_order_fields.get("status") or "pending"))

        order_doc = dict(base_order_fields)
        order_doc["order_id"] = line_order_id
        order_doc["items"] = [item]
        order_doc["total_amount"] = total_amount
        order_doc["charged_amount"] = total_amount if status != "skipped" else 0.0
        order_doc["profit_amount_total"] = profit_amount_total
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

        if _checkout_helpers.get("order_fn"):
            try:
                _checkout_helpers["order_fn"](orders_col, order_doc)
            except Exception:
                orders_col.insert_one(order_doc)
        else:
            orders_col.insert_one(order_doc)
        created_docs.append(order_doc)

        if line_ref and line_ref in job_map:
            order_jobs.append((line_order_id, [job_map[line_ref]]))

    return created_docs, order_jobs


def _credit_paid_store_order_profits(
    store_slug: str,
    order_docs: List[Dict[str, Any]],
    payment_status: str,
) -> float:
    """Credit each paid store order once, including split-cart orders."""
    if str(payment_status or "").strip().lower() != "paid":
        return 0.0

    credited_total = 0.0
    for order_doc in order_docs or []:
        order_id = str(order_doc.get("order_id") or "").strip()
        store_profit = round(
            sum(_money(item.get("store_profit_amount")) for item in (order_doc.get("items") or [])),
            2,
        )
        if not order_id or store_profit <= 0:
            continue

        now = datetime.utcnow()
        store_accounts_col.update_one(
            {"store_slug": store_slug},
            {
                "$setOnInsert": {
                    "store_slug": store_slug,
                    "total_profit_balance": 0.0,
                    "created_at": now,
                }
            },
            upsert=True,
        )
        result = store_accounts_col.update_one(
            {
                "store_slug": store_slug,
                "credited_order_ids": {"$ne": order_id},
            },
            {
                "$inc": {"total_profit_balance": store_profit},
                "$set": {
                    "last_updated_profit": store_profit,
                    "updated_at": now,
                },
                "$addToSet": {"credited_order_ids": order_id},
            },
            upsert=False,
        )
        if result.modified_count == 1:
            credited_total += store_profit

    return round(credited_total, 2)

NETWORK_ID_FALLBACK: Dict[str, int] = {
    "MTN": 3,
    "VODAFONE": 2,
    "AIRTELTIGO": 1,
}


# ---------------------------------------------------------------------
# Small utils
# ---------------------------------------------------------------------
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

def _slugify(s: str) -> str:
    s2 = (s or "").lower().strip()
    s2 = re.sub(r"[^a-z0-9]+", "-", s2).strip("-")
    return s2 or "store"

def _absolute_store_url(value: Any) -> str:
    """
    Return a public absolute URL for store assets used by social crawlers.
    Store uploads are saved as /media/<id>, which WhatsApp cannot use unless
    og:image is fully qualified.
    """
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if re.match(r"^https?://", url, flags=re.I):
        return url
    return urljoin(f"https://{TARGET_STORE_HOST}/", url.lstrip("/"))

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()
    availability = (svc.get("availability") or "AVAILABLE").upper()
    closed_msg = svc.get("closed_message") or "This service is temporarily closed."
    oos_msg = svc.get("out_of_stock_message") or "This service is currently out of stock."
    can_order = t in {"API", "OFF", "MANUAL"} and status == "OPEN" and availability == "AVAILABLE"
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
        "can_order": can_order,
        "disabled_reason": disabled_reason,
    }

def _sorted_services(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def prio_tuple(s: Dict[str, Any]) -> Tuple[float, float, float, str]:
        prio = _to_float(s.get("priority")) or float("inf")
        nrank = _name_rank(s.get("name") or "")
        nrank = nrank if nrank is not None else 10_000
        display_order = _to_float(s.get("display_order")) or float("inf")
        created = s.get("created_at")
        ts = 0.0
        if isinstance(created, datetime):
            ts = -created.timestamp()
        else:
            try:
                v = float(created)
                ts = -(v / 1000.0 if v > 1e12 else v)
            except Exception:
                ts = 0.0
        alpha = _norm(s.get("name") or "")
        return (prio, nrank, display_order, ts, alpha)

    raw.sort(key=prio_tuple)
    return raw


def _results_checker_type(value_obj: Any, value_raw: Any = None) -> str:
    candidates: List[Any] = []
    if value_obj is not None:
        candidates.append(value_obj)
    if value_raw is not None:
        candidates.append(value_raw)

    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("type", "checker_type", "id", "value"):
                val = str(candidate.get(key) or "").strip().lower()
                if val in {"wassce", "bece"}:
                    return val
        else:
            val = str(candidate or "").strip().lower()
            if val in {"wassce", "bece"}:
                return val
    return ""


def _results_checker_stock_rows() -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    for checker_type in ("wassce", "bece"):
        doc = wassce_col.find_one(
            {"type": checker_type, "status": "not_sold"},
            sort=[("created_at", 1)],
        )
        if not doc:
            continue
        amount = _to_float(doc.get("amount"))
        if amount is None or amount <= 0:
            continue
        rows[checker_type] = {
            "type": checker_type,
            "amount": round(float(amount), 2),
            "message": str(doc.get("message") or ""),
            "_id": doc.get("_id"),
        }
    return rows


def _build_results_checker_service() -> Dict[str, Any]:
    stock = _results_checker_stock_rows()
    offers: List[Dict[str, Any]] = []
    for checker_type in ("wassce", "bece"):
        row = stock.get(checker_type)
        if not row:
            continue
        offers.append(
            {
                "amount": row["amount"],
                "total": row["amount"],
                "value": {"type": checker_type, "checker_type": checker_type},
                "value_text": checker_type.upper(),
            }
        )

    svc: Dict[str, Any] = {
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
        "store_offers": offers,
        "default_profit_percent": 0.0,
        "store_display": "ON",
        "display": "ON",
    }
    svc.update(_service_state(svc))
    return svc


# ---------------------------------------------------------------------
# ✅ WhatsApp helpers
# ---------------------------------------------------------------------
def _wa_digits(v: Any) -> str:
    d = re.sub(r"\D+", "", str(v or ""))
    if d.startswith("0") and len(d) == 10:
        return "233" + d[1:]
    if d.startswith("233") and len(d) == 12:
        return d
    return d

def _wa_link_from_number(raw: Any, text: str = "") -> str:
    d = _wa_digits(raw)
    if not d:
        return ""
    msg = (text or "").strip()
    if msg:
        try:
            from urllib.parse import quote
            return f"https://wa.me/{d}?text={quote(msg)}"
        except Exception:
            return f"https://wa.me/{d}"
    return f"https://wa.me/{d}"

def _extract_store_whatsapp(store_doc: Dict[str, Any]) -> Dict[str, str]:
    def pick(*paths) -> Any:
        for p in paths:
            cur = store_doc
            ok = True
            for key in p:
                if not isinstance(cur, dict) or key not in cur:
                    ok = False
                    break
                cur = cur.get(key)
            if ok and cur not in (None, "", [], {}):
                return cur
        return ""

    wa_number = pick(
        ("whatsapp_number",),
        ("contact", "whatsapp_number"),
        ("hero", "whatsapp_number"),
        ("theme", "whatsapp_number"),
        ("whatsapp", "number"),
    )
    wa_group = pick(
        ("whatsapp_group",),
        ("contact", "whatsapp_group"),
        ("hero", "whatsapp_group"),
        ("theme", "whatsapp_group"),
        ("whatsapp", "group"),
        ("whatsapp_group_link",),
        ("contact", "whatsapp_group_link"),
    )

    wa_number_str = str(wa_number or "").strip()
    wa_group_str = str(wa_group or "").strip()

    return {
        "number_raw": wa_number_str,
        "number_digits": _wa_digits(wa_number_str),
        "number_link": _wa_link_from_number(
            wa_number_str, f"Hello {store_doc.get('name','')}, I want to order."
        ),
        "group_link": wa_group_str,
    }


# =====================================================================
# ✅ Offers source:
# - Page pricing: store_offers authoritative, fallback to offers
# =====================================================================
def _svc_offers_list(svc: Dict[str, Any]) -> List[Dict[str, Any]]:
    so = svc.get("store_offers")
    if isinstance(so, list) and so:
        return so
    off = svc.get("offers")
    if isinstance(off, list) and off:
        return off
    return []

def _offer_base_amount(of: Dict[str, Any]) -> Optional[float]:
    if not isinstance(of, dict):
        return None
    v = of.get("store_amount")
    base = _to_float(v)
    if base is not None:
        return base
    return _to_float(of.get("amount"))


# =====================================================================
# ✅ NEW PROFIT RULE HELPERS (PRO, SAFE)
# =====================================================================
def _effective_store_profit_percent(svc_doc: Optional[Dict[str, Any]]) -> float:
    """
    Store checkout profit percent.
    Priority:
      1) svc_doc.store_offers_profit
      2) svc_doc.default_profit_percent
      3) 0.0
    """
    if not svc_doc:
        return 0.0
    try:
        v = svc_doc.get("store_offers_profit")
        if v is not None and str(v).strip() != "":
            return float(v)
    except Exception:
        pass
    try:
        v2 = svc_doc.get("default_profit_percent")
        if v2 is not None and str(v2).strip() != "":
            return float(v2)
    except Exception:
        pass
    return 0.0


# ✅ UPDATED: products loader (NOW loads from store_products_col first)
def _load_store_products(store_doc: Dict[str, Any], wa_number_raw: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    def _safe_float(v: Any) -> float:
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return 0.0

    def _safe_int(v: Any) -> int:
        try:
            return int(float(str(v).replace(",", "").strip()))
        except Exception:
            return 0

    def _pick_img(p: Dict[str, Any]) -> str:
        return (
            (p.get("image_url") or p.get("image") or p.get("img") or p.get("photo") or "")
            if isinstance(p, dict)
            else ""
        )

    def _pick_name(p: Dict[str, Any]) -> str:
        return (p.get("name") or p.get("title") or p.get("product_name") or "Product").strip()

    def _pick_desc(p: Dict[str, Any]) -> str:
        return (p.get("description") or p.get("desc") or "").strip()

    def _pick_price(p: Dict[str, Any]) -> float:
        for k in ("price", "amount", "selling_price", "unit_price"):
            if k in p and p.get(k) not in (None, ""):
                return _safe_float(p.get(k))
        return 0.0

    def _pick_qty(p: Dict[str, Any]) -> int:
        for k in ("quantity", "qty", "stock"):
            if k in p and p.get(k) not in (None, ""):
                return _safe_int(p.get(k))
        return 0

    def _product_order_link(pname: str, price: float) -> str:
        msg = f"Hello {store_doc.get('name','')}, I want to order: {pname}"
        if price and price > 0:
            msg += f" (GHS {price:.2f})"
        msg += "."
        return _wa_link_from_number(wa_number_raw, msg)

    slug = store_doc.get("slug")
    owner_id = store_doc.get("owner_id")
    store_id = store_doc.get("_id")

    # 1) ✅ MAIN: store_products collection
    try:
        q_candidates: List[Dict[str, Any]] = []
        if slug:
            q_candidates.append({"store_slug": slug, "status": {"$ne": "deleted"}})
        if store_id:
            q_candidates.append({"store_id": store_id, "status": {"$ne": "deleted"}})
            q_candidates.append({"store_id": str(store_id), "status": {"$ne": "deleted"}})
        if owner_id:
            q_candidates.append({"owner_id": owner_id, "status": {"$ne": "deleted"}})
            q_candidates.append({"owner_id": str(owner_id), "status": {"$ne": "deleted"}})

        fields = {
            "_id": 1,
            "store_slug": 1,
            "store_id": 1,
            "owner_id": 1,
            "manager_id": 1,
            "name": 1,
            "description": 1,
            "image_url": 1,
            "price": 1,
            "quantity": 1,
            "status": 1,
            "created_at": 1,
            "updated_at": 1,
        }

        found: List[Dict[str, Any]] = []
        for q in q_candidates:
            try:
                if store_products_col.count_documents(q, limit=1) > 0:
                    found = list(store_products_col.find(q, fields).sort("created_at", -1))
                    break
            except Exception:
                continue

        if found:
            for p in found:
                pname = _pick_name(p)
                price = _pick_price(p)
                out.append(
                    {
                        "_id_str": str(p.get("_id") or ""),
                        "name": pname,
                        "description": _pick_desc(p),
                        "image_url": _pick_img(p),
                        "price": round(price, 2),
                        "quantity": _pick_qty(p),
                        "created_at": p.get("created_at") or None,
                        "order_link": _product_order_link(pname, price) if wa_number_raw else "",
                    }
                )
            return out
    except Exception:
        pass

    # 2) embedded on store doc (if any)
    embedded = store_doc.get("products")
    if isinstance(embedded, list) and embedded:
        for p in embedded:
            if not isinstance(p, dict):
                continue
            pname = _pick_name(p)
            price = _pick_price(p)
            out.append(
                {
                    "_id_str": str(p.get("_id") or ""),
                    "name": pname,
                    "description": _pick_desc(p),
                    "image_url": _pick_img(p),
                    "price": round(price, 2),
                    "quantity": _pick_qty(p),
                    "created_at": p.get("created_at") or None,
                    "order_link": _product_order_link(pname, price) if wa_number_raw else "",
                }
            )
        return out

    # 3) legacy: products collection fallback
    try:
        q_candidates2: List[Dict[str, Any]] = []
        if slug:
            q_candidates2.append({"store_slug": slug, "status": {"$ne": "deleted"}})
        if store_id:
            q_candidates2.append({"store_id": store_id, "status": {"$ne": "deleted"}})
            q_candidates2.append({"store_id": str(store_id), "status": {"$ne": "deleted"}})
        if owner_id:
            q_candidates2.append({"owner_id": owner_id, "status": {"$ne": "deleted"}})
            q_candidates2.append({"owner_id": str(owner_id), "status": {"$ne": "deleted"}})

        fields2 = {
            "_id": 1,
            "name": 1,
            "title": 1,
            "description": 1,
            "image_url": 1,
            "image": 1,
            "price": 1,
            "amount": 1,
            "selling_price": 1,
            "unit_price": 1,
            "quantity": 1,
            "created_at": 1,
            "status": 1,
        }

        found2: List[Dict[str, Any]] = []
        for q in q_candidates2:
            try:
                if products_col.count_documents(q, limit=1) > 0:
                    found2 = list(products_col.find(q, fields2).sort("created_at", -1))
                    break
            except Exception:
                continue

        for p in found2:
            pname = (p.get("name") or p.get("title") or "Product").strip()
            price = 0.0
            for k in ("price", "amount", "selling_price", "unit_price"):
                if k in p and p.get(k) not in (None, ""):
                    try:
                        price = float(str(p.get(k)).replace(",", "").strip())
                    except Exception:
                        price = 0.0
                    break
            out.append(
                {
                    "_id_str": str(p.get("_id") or ""),
                    "name": pname,
                    "description": (p.get("description") or "").strip(),
                    "image_url": (p.get("image_url") or p.get("image") or "").strip(),
                    "price": round(price, 2),
                    "quantity": 0,
                    "created_at": p.get("created_at") or None,
                    "order_link": _wa_link_from_number(
                        wa_number_raw,
                        f"Hello {store_doc.get('name','')}, I want to order: {pname} (GHS {price:.2f}).",
                    )
                    if wa_number_raw
                    else "",
                }
            )
    except Exception:
        return []

    return out


# ---------------------------------------------------------------------
# Parse + labels
# ---------------------------------------------------------------------
_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)

def _service_unit(svc: Dict[str, Any]) -> str:
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes") or name == "afa talktime":
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
                    data = ast.literal_eval(vt)
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    if isinstance(value, dict):
        vol = value.get("volume") or value.get("offer") or value.get("gb")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            v = float(vol)
            if unit == "minutes":
                return v
            vol_s = str(vol).upper()
            if "GB" in vol_s:
                return v * 1000.0
            if "MB" in vol_s:
                return v
            return v
        vol_s = str(vol)
        if unit == "minutes":
            m = _MIN.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)
            return None
        else:
            m = _GB.search(vol_s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)
            return None

    if isinstance(value, str):
        s = value
        if unit == "minutes":
            m = _MIN.search(s)
            if m:
                return float(m.group(1))
            if _NUM.match(s):
                return float(s)
            s2 = _PKG_TAIL.sub("", s)
            m = _MIN.search(s2)
            if m:
                return float(m.group(1))
            return None
        else:
            m = _GB.search(s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s)
            if m:
                return float(m.group(1))
            s2 = _PKG_TAIL.sub("", s)
            m = _GB.search(s2)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s2)
            if m:
                return float(m.group(1))
            if _NUM.match(s2):
                return float(s2)
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
        parsed = _parse_value_field(cleaned)
        if isinstance(parsed, dict):
            vol = _extract_volume(parsed, unit)
            return _format_volume_unit(vol, unit) if vol is not None else "-"
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"


# ---------- pricing map builder ----------
def _build_pricing_map(pricing: Dict[str, Any]) -> Tuple[float, Dict[str, Dict[str, Any]]]:
    percent_default = float(pricing.get("percent_default") or 0.0)
    per_map: Dict[str, Dict[str, Any]] = {}
    for x in (pricing.get("per_service") or []):
        sid = str(x.get("service_id") or "")
        if not sid:
            continue
        entry: Dict[str, Any] = {"percent": None, "offers": {}}
        if x.get("percent") is not None:
            try:
                entry["percent"] = float(x.get("percent"))
            except Exception:
                entry["percent"] = None
        for o in (x.get("offers") or []):
            try:
                idx = int(o.get("index"))
                tot = _to_float(o.get("total"))
                if tot is not None:
                    entry["offers"][idx] = float(tot)
            except Exception:
                continue
        per_map[sid] = entry
    return percent_default, per_map


# ---------- apply pricing to a service (for page render) ----------
def _offer_value_text(o: Dict[str, Any], unit: str) -> str:
    vt = o.get("value_text")
    if isinstance(vt, str) and vt.strip():
        try:
            cleaned = _PKG_TAIL.sub("", vt).strip()
            vol = _extract_volume(cleaned, unit)
            if vol is not None:
                return _format_volume_unit(vol, unit)
        except Exception:
            pass
    lab = _value_text_for_display(o.get("value"), unit)
    return lab or "-"

def _apply_store_pricing_to_service(
    svc: Dict[str, Any],
    percent_default: float,
    per_service_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    s = dict(svc)
    unit = _service_unit(s)
    src_offers = _svc_offers_list(s)
    svc_id_str = str(s.get("_id_str") or s.get("_id") or "")
    per_entry = per_service_map.get(svc_id_str, {})
    offer_overrides: Dict[int, float] = per_entry.get("offers") or {}

    norm_offers: List[Dict[str, Any]] = []
    for idx, of in enumerate(src_offers):
        base_amount = _offer_base_amount(of)
        if idx in offer_overrides:
            total = round(float(offer_overrides[idx]), 2)
        else:
            total = round(float(base_amount), 2) if base_amount is not None else None
        vt = _offer_value_text(of, unit)
        norm_offers.append(
            {
                "value_text": vt,
                "total": total,
                "amount": base_amount,
                "value": of.get("value"),
            }
        )

    s["offers"] = norm_offers
    s["offers_source"] = "store_offers" if (isinstance(s.get("store_offers"), list) and s.get("store_offers")) else "offers"
    return s


# ---------- DB loads for editor/view ----------
def _load_all_services_for_store_edit() -> List[Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    fields = {"_id": 1, "name": 1, "offers": 1, "store_offers": 1, "unit": 1, "display": 1, "store_display": 1}
    raw = list(services_col.find({"display": {"$ne": "OFF"}, "store_display": {"$ne": "OFF"}}, fields))
    raw.sort(key=lambda x: _norm(x.get("name") or ""))

    clean: List[Dict[str, Any]] = []
    for r in raw:
        s: Dict[str, Any] = {"_id_str": str(r.get("_id")), "name": r.get("name") or ""}
        unit = _service_unit(r)
        src_offers = _svc_offers_list(r)

        new_off: List[Dict[str, Any]] = []
        for o in src_offers:
            new_off.append(
                {
                    "amount": _offer_base_amount(o),
                    "value": o.get("value"),
                    "value_text": _offer_value_text(o, unit),
                }
            )

        s["offers"] = new_off
        s["offers_source"] = "store_offers" if (isinstance(r.get("store_offers"), list) and r.get("store_offers")) else "offers"
        clean.append(s)
    clean.append(_build_results_checker_service())
    return clean

def _load_services_for_store_view(scope: str, ids: List[str]) -> List[Dict[str, Any]]:
    include_results_checker = False
    q: Dict[str, Any] = {}
    if scope == "selected" and ids:
        valid_ids: List[ObjectId] = []
        for raw_id in ids:
            sid = str(raw_id or "").strip()
            if not sid:
                continue
            if sid == RESULTS_CHECKER_SERVICE_ID:
                include_results_checker = True
                continue
            try:
                valid_ids.append(ObjectId(sid))
            except Exception:
                continue
        q = {"_id": {"$in": valid_ids}, "display": {"$ne": "OFF"}, "store_display": {"$ne": "OFF"}}
    else:
        q = {"display": {"$ne": "OFF"}, "store_display": {"$ne": "OFF"}}

    fields = {
        "_id": 1,
        "name": 1,
        "type": 1,
        "status": 1,
        "availability": 1,
        "image_url": 1,
        "offers": 1,
        "store_offers": 1,
        "store_offers_profit": 1,  # ✅ IMPORTANT for profit logic
        "service_category": 1,
                            "provider": 1,
        "priority": 1,
        "display_order": 1,
        "created_at": 1,
        "unit": 1,
        "default_profit_percent": 1,
        "network_id": 1,
        "network": 1,
        "closed_message": 1,
        "out_of_stock_message": 1,
        "display": 1,
        "store_display": 1,
    }
    raw = list(services_col.find(q, fields))
    raw = _sorted_services(raw)
    for s in raw:
        s["_id_str"] = str(s["_id"])
        s.update(_service_state(s))
    if include_results_checker:
        raw.append(_build_results_checker_service())
    return raw

def _load_products_as_services_fallback(store_doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    try:
        q: Dict[str, Any] = {"status": {"$ne": "deleted"}}
        if store_doc.get("slug"):
            q_alt = {"store_slug": store_doc.get("slug"), "status": {"$ne": "deleted"}}
            if products_col.count_documents(q_alt, limit=1) > 0:
                q = q_alt
        if store_doc.get("owner_id"):
            q_owner = {"owner_id": store_doc.get("owner_id"), "status": {"$ne": "deleted"}}
            if products_col.count_documents(q_owner, limit=1) > 0:
                q = q_owner

        fields = {"_id": 1, "name": 1, "title": 1, "image_url": 1, "price": 1, "amount": 1, "created_at": 1}
        prods = list(products_col.find(q, fields).sort("created_at", -1))
        out: List[Dict[str, Any]] = []
        for p in prods:
            name = (p.get("name") or p.get("title") or "Product").strip()
            price = _to_float(p.get("price")) or _to_float(p.get("amount")) or 0.0
            svc = {
                "_id": p.get("_id"),
                "_id_str": str(p.get("_id")),
                "name": name,
                "type": "MANUAL",
                "status": "OPEN",
                "availability": "AVAILABLE",
                "image_url": p.get("image_url"),
                "service_category": "product",
                "priority": None,
                "display_order": None,
                "created_at": p.get("created_at") or datetime.utcnow(),
                "unit": "item",
                "offers": [
                    {
                        "value_text": "1 item",
                        "total": round(float(price), 2),
                        "amount": round(float(price), 2),
                        "value": {"volume": 1},
                    }
                ],
            }
            svc.update(_service_state(svc))
            out.append(svc)
        return out
    except Exception:
        return []


# ---------- NEW: safe ObjectId + user lookup (NO status filter) ----------
def _safe_oid(v: Any) -> Optional[ObjectId]:
    if not v:
        return None
    if isinstance(v, ObjectId):
        return v
    if isinstance(v, str):
        try:
            return ObjectId(v)
        except Exception:
            return None
    return None

def _lookup_user_any_status(user_id: Any) -> Dict[str, Any]:
    """
    Fetch user by _id WITHOUT filtering status.
    """
    oid = _safe_oid(user_id)
    if not oid:
        return {}
    try:
        u = users_col.find_one(
            {"_id": oid},
            {"email": 1, "phone": 1, "username": 1, "first_name": 1, "last_name": 1, "name": 1, "status": 1},
        )
        return u or {}
    except Exception:
        return {}

def _user_first_last(u: Dict[str, Any]) -> Tuple[str, str]:
    """
    Derive first/last from first_name/last_name, or from 'name' if present.
    """
    first = (u.get("first_name") or "").strip()
    last = (u.get("last_name") or "").strip()
    if first or last:
        return first, last

    full = (u.get("name") or u.get("username") or "").strip()
    if not full:
        return "", ""
    parts = [p for p in re.split(r"\s+", full) if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ---------- JSON-safe converter (UPDATED to include owner email/phone safely) ----------
def _store_to_client(s: Optional[dict]) -> dict:
    if not s:
        return {}
    out: Dict[str, Any] = {}
    for k, v in s.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [
                (str(x) if isinstance(x, ObjectId) else x.isoformat() if isinstance(x, datetime) else x)
                for x in v
            ]
        elif isinstance(v, dict):
            if k == "pricing":
                per = []
                for row in (v.get("per_service") or []):
                    row2 = dict(row)
                    if isinstance(row2.get("service_id"), ObjectId):
                        row2["service_id"] = str(row2["service_id"])
                    per.append(row2)
                out[k] = {**v, "per_service": per}
            else:
                out[k] = {
                    kk: (
                        str(vv)
                        if isinstance(vv, ObjectId)
                        else vv.isoformat()
                        if isinstance(vv, datetime)
                        else vv
                    )
                    for kk, vv in v.items()
                }
        else:
            out[k] = v
    if "service_ids" in out:
        out["service_ids"] = [str(x) for x in (out.get("service_ids") or [])]

    # ✅ attach owner info from users collection (even if user.status == 'deleted')
    try:
        u = _lookup_user_any_status(s.get("owner_id"))
        out["owner_email"] = (u.get("email") or "").strip()
        out["owner_phone"] = (u.get("phone") or "").strip()
        out["owner_username"] = (u.get("username") or "").strip()
        out["owner_status"] = (u.get("status") or "").strip()
        fn, ln = _user_first_last(u or {})
        out["owner_first_name"] = fn
        out["owner_last_name"] = ln
    except Exception:
        out["owner_email"] = out.get("owner_email") or ""
        out["owner_phone"] = out.get("owner_phone") or ""
        out["owner_first_name"] = out.get("owner_first_name") or ""
        out["owner_last_name"] = out.get("owner_last_name") or ""

    return out


# ---------- helper: find current user's store ----------
def _find_user_store(user_id: ObjectId, slug: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    q: Dict[str, Any] = {"owner_id": user_id, "status": {"$ne": "deleted"}}
    if slug:
        q["slug"] = slug
    return stores_col.find_one(q, sort=[("updated_at", -1), ("created_at", -1)])


# ---------- compatibility helper: _find (some files import it) ----------
def _find(col, q: dict, projection: Optional[dict] = None, sort: Optional[list] = None):
    """
    Compatibility helper (kept to prevent ImportError in files that do:
      from .store_page import _find
    """
    try:
        if sort:
            return col.find_one(q, projection or None, sort=sort)
        return col.find_one(q, projection or None)
    except Exception:
        return None


# ---------- helper: store owner's email (UPDATED: no status filter) ----------
def _get_owner_email_for_store(store_doc: Dict[str, Any]) -> str:
    try:
        oid2 = _safe_oid(store_doc.get("owner_id"))
        if not oid2:
            return ""
        u = users_col.find_one({"_id": oid2}, {"email": 1})
        if not u:
            return ""
        return (u.get("email") or "").strip()
    except Exception:
        return ""

def _get_owner_identity_for_store(store_doc: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    ✅ Paystack payer identity MUST come from DB (no fallback defaults).
    Returns (email, first_name, last_name)
    """
    try:
        u = _lookup_user_any_status(store_doc.get("owner_id"))
        email = (u.get("email") or "").strip()
        first, last = _user_first_last(u or {})
        return email, (first or "").strip(), (last or "").strip()
    except Exception:
        return "", "", ""


def _get_current_payer_identity_for_store(store_doc: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Prefer the current logged-in buyer identity for Paystack.
    For guest buyers, keep payer names empty instead of submitting the store
    owner's name as the customer identity.
    """
    try:
        session_user_id = session.get("user_id")
        if session_user_id:
            user_doc = _lookup_user_any_status(session_user_id)
            email = (user_doc.get("email") or "").strip()
            first, last = _user_first_last(user_doc or {})
            return email, (first or "").strip(), (last or "").strip()
    except Exception:
        pass
    return "", "", ""


# ---------- shared upsert ----------
def _upsert_store_from_payload(owner_id: ObjectId, data: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """
    ✅ IMPORTANT: This function is imported by routes/store_create.py
    DO NOT remove/rename it.
    """
    name = (data.get("name") or "").strip()
    slug = _slugify(data.get("slug") or name)
    status = (data.get("status") or "published").strip()
    if not name or not slug:
        return False, {"message": "Name and slug are required"}

    existing = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
    if existing and str(existing.get("owner_id")) != str(owner_id):
        return False, {"message": "Slug already taken"}

    doc = {
        "owner_id": owner_id,
        "name": name,
        "slug": slug,
        "logo_url": (data.get("logo_url") or "").strip(),
        "layout": (data.get("layout") or "grid-2").strip(),
        "theme": data.get("theme") or {},
        "hero": data.get("hero") or {},
        "service_scope": data.get("service_scope") or "all",
        "service_ids": data.get("service_ids") or [],
        "pricing": data.get("pricing") or {"mode": "manual", "percent_default": 0.0, "per_service": []},
        "products": data.get("products") or data.get("store_products") or data.get("items") or [],
        "whatsapp_number": (data.get("whatsapp_number") or data.get("whatsapp") or "").strip()
        if isinstance(data.get("whatsapp_number") or data.get("whatsapp") or "", str)
        else data.get("whatsapp_number") or data.get("whatsapp"),
        "whatsapp_group": (data.get("whatsapp_group") or data.get("whatsapp_group_link") or "").strip(),
        "status": status,
        "updated_at": datetime.utcnow(),
    }
    stores_col.update_one(
        {"slug": slug, "owner_id": owner_id},
        {"$set": doc, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True,
    )
    return True, {"slug": slug, "status": status}


# =====================================================================
# PAGES (PUBLIC)
# =====================================================================
@stores_bp.route("/s/<slug>", methods=["GET"])
def store_public_page(slug: str):
    redir = _store_host_redirect()
    if redir:
        return redir
    store_doc = stores_col.find_one(
        {"slug": slug, "status": {"$regex": r"^published$", "$options": "i"}}
    )
    if not store_doc:
        # allow preview=1 for logged-in owner
        if request.args.get("preview") == "1" and session.get("user_id"):
            store_doc = stores_col.find_one(
                {"slug": slug, "owner_id": ObjectId(session["user_id"]), "status": {"$ne": "deleted"}}
            )
            if not store_doc:
                return "Store not found", 404
        else:
            return "Store not found", 404

    scope = store_doc.get("service_scope") or "all"
    service_ids = store_doc.get("service_ids") or []
    services = _load_services_for_store_view(scope, service_ids)

    # legacy fallback (only if you were using products as services)
    if not services:
        services = _load_products_as_services_fallback(store_doc)

    percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    priced = [_apply_store_pricing_to_service(s, percent_default, per_map) for s in services]

    q = request.query_string.decode("utf-8")
    canonical_url = f"https://{TARGET_STORE_HOST}{request.path}" + (f"?{q}" if q else "")
    store_name = (store_doc.get("name") or "Store").strip()
    share_image_url = _absolute_store_url(store_doc.get("logo_url")) or DEFAULT_STORE_SHARE_IMAGE
    share_title = store_name
    share_description = f"Shop bundles & services from {store_name}"

    wa = _extract_store_whatsapp(store_doc)

    # ✅ REAL products list for the Products tab
    products = _load_store_products(store_doc, wa.get("number_raw") or "")

    # ✅ Ensure we never pass secret key to frontend
    pk_for_frontend = PAYSTACK_PUBLIC_KEY if _is_pk(PAYSTACK_PUBLIC_KEY) else ""

    # ✅ Fetch owner identity for Paystack payer identity (NO DEFAULTS)
    ps_email, ps_first, ps_last = _get_current_payer_identity_for_store(store_doc)

    # ✅ Fetch email (store email + owner email) for extra context if needed
    owner_email = _get_owner_email_for_store(store_doc)

    return render_template(
        "store_page.html",
        announcements=get_active_announcements("store_page", store_slug=slug),
        store=store_doc,
        services=priced,
        products=products,
        paystack_pk=pk_for_frontend,
        canonical_url=canonical_url,
        share_title=share_title,
        share_description=share_description,
        share_image_url=share_image_url,
        whatsapp_number=wa.get("number_raw") or "",
        whatsapp_number_digits=wa.get("number_digits") or "",
        whatsapp_number_link=wa.get("number_link") or "",
        whatsapp_group_link=wa.get("group_link") or "",
        enforce_known_number_check=_known_number_enforcement_enabled(),
        # extra fields (won't break template even if unused)
        store_email=(store_doc.get("email") or "").strip(),
        owner_email=owner_email,

        # ✅ REQUIRED by your HTML scripts (NO DEFAULTS)
        paystack_payer_email=ps_email,
        paystack_payer_first=ps_first,
        paystack_payer_last=ps_last,
    )


# ✅ API: fetch store email (and owner email) without touching HTML
@stores_bp.route("/api/store-email/<slug>", methods=["GET"])
def api_store_email(slug: str):
    try:
        store_doc = stores_col.find_one(
            {"slug": slug, "status": {"$ne": "deleted"}},
            {"email": 1, "owner_id": 1, "slug": 1, "name": 1},
        )
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404
        owner_email = _get_owner_email_for_store(store_doc)
        return jsonify(
            {
                "success": True,
                "slug": slug,
                "store_name": store_doc.get("name") or "",
                "store_email": (store_doc.get("email") or "").strip(),
                "owner_email": owner_email,
            }
        ), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


# ✅ API: Store products payload builder
def _products_payload(store_doc: Dict[str, Any]) -> Dict[str, Any]:
    wa = _extract_store_whatsapp(store_doc or {})
    products = _load_store_products(store_doc or {}, wa.get("number_raw") or "")
    return {
        "success": True,
        "store": {
            "slug": store_doc.get("slug") or "",
            "name": store_doc.get("name") or "",
            "logo_url": store_doc.get("logo_url") or "",
            "status": store_doc.get("status") or "",
            "owner_id": str(store_doc.get("owner_id")) if store_doc.get("owner_id") else "",
        },
        "whatsapp": {
            "number_raw": wa.get("number_raw") or "",
            "number_digits": wa.get("number_digits") or "",
            "number_link": wa.get("number_link") or "",
            "group_link": wa.get("group_link") or "",
        },
        "count": len(products),
        "products": products,
    }

@stores_bp.route("/api/store-products/<slug>", methods=["GET"])
def api_store_products_by_slug(slug: str):
    """
    Frontend-friendly products API.
    - Returns products created for this store (store_products primary, then fallbacks).
    - Optional: ?owner_id=<id> or ?manager_id=<id> (filters if you use those fields)
    """
    try:
        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        owner_id = (request.args.get("owner_id") or "").strip()
        manager_id = (request.args.get("manager_id") or "").strip()

        if owner_id or manager_id:
            q: Dict[str, Any] = {"store_slug": slug, "status": {"$ne": "deleted"}}
            if owner_id:
                q["owner_id"] = owner_id
            if manager_id:
                q["manager_id"] = manager_id

            fields = {
                "_id": 1,
                "name": 1,
                "description": 1,
                "image_url": 1,
                "price": 1,
                "quantity": 1,
                "created_at": 1,
                "updated_at": 1,
            }

            found = list(store_products_col.find(q, fields).sort("created_at", -1))
            wa = _extract_store_whatsapp(store_doc)
            products: List[Dict[str, Any]] = []
            for p in found:
                try:
                    price = float(str(p.get("price") or "0").replace(",", "").strip())
                except Exception:
                    price = 0.0
                pname = (p.get("name") or "Product").strip()
                qty_raw = p.get("quantity")
                try:
                    qty = int(float(str(qty_raw).replace(",", "").strip())) if str(qty_raw or "").strip() != "" else 0
                except Exception:
                    qty = 0

                products.append(
                    {
                        "_id_str": str(p.get("_id") or ""),
                        "name": pname,
                        "description": (p.get("description") or "").strip(),
                        "image_url": (p.get("image_url") or "").strip(),
                        "price": round(price, 2),
                        "quantity": qty,
                        "created_at": p.get("created_at") or None,
                        "order_link": _wa_link_from_number(
                            wa.get("number_raw") or "",
                            f"Hello {store_doc.get('name','')}, I want to order: {pname} (GHS {price:.2f}).",
                        )
                        if (wa.get("number_raw") or "")
                        else "",
                    }
                )

            payload = _products_payload(store_doc)
            payload["products"] = products
            payload["count"] = len(products)
            payload["filters"] = {"owner_id": owner_id, "manager_id": manager_id}
            return jsonify(payload), 200

        return jsonify(_products_payload(store_doc)), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500

@stores_bp.route("/api/store-products/by-owner/<owner_id>", methods=["GET"])
def api_store_products_by_owner(owner_id: str):
    """
    Useful for dashboards:
    GET /api/store-products/by-owner/<owner_id>
    Optional: ?slug=<store_slug>
    """
    try:
        owner_id = (owner_id or "").strip()
        if not owner_id:
            return jsonify({"success": False, "message": "owner_id required"}), 400

        slug = (request.args.get("slug") or "").strip()

        store_q: Dict[str, Any] = {"status": {"$ne": "deleted"}}
        oid = _safe_oid(owner_id)
        if oid:
            store_q["owner_id"] = oid
        else:
            store_q["owner_id"] = owner_id

        if slug:
            store_q["slug"] = slug

        store_doc = stores_col.find_one(store_q, sort=[("updated_at", -1), ("created_at", -1)])
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found for owner"}), 404

        return jsonify(_products_payload(store_doc)), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500


# =====================================================================
# PAYSTACK FLOW (Store)
# =====================================================================
def _verify_paystack(reference: str) -> Tuple[bool, Dict[str, Any], str]:
    if not PAYSTACK_SECRET_KEY or not _is_sk(PAYSTACK_SECRET_KEY):
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

DUP_WINDOW_MINUTES = 30

def _normalize_amount_key(v):
    try:
        return float(f"{float(v):.2f}")
    except Exception:
        return 0.0

def _build_bundle_key(is_express: bool, shared_bundle, value_obj: dict):
    if is_express:
        val = shared_bundle
        if val is None:
            val = (value_obj or {}).get("id")
        try:
            return ("offer", int(val)) if val is not None else None
        except Exception:
            return None
    else:
        val = shared_bundle
        if val is None:
            val = (value_obj or {}).get("volume")
        try:
            return ("vol", int(val)) if val is not None else None
        except Exception:
            return None

def _has_processing_conflict_strict(
    phone: str,
    service_id_raw: str | None,
    svc_name: str | None,
    network_id: int | None,
    bundle_key: tuple | None,
    amount_key: float,
) -> bool:
    if not phone or network_id is None or bundle_key is None:
        return False
    window_start = datetime.utcnow() - timedelta(minutes=DUP_WINDOW_MINUTES)
    kind, bval = bundle_key

    elem = {
        "phone": phone,
        "network_id": network_id,
        "bundle_key.kind": kind,
        "bundle_key.value": bval,
        "amount": amount_key,
    }
    if service_id_raw:
        elem["serviceId"] = service_id_raw

    q = {
        "status": {"$in": ["processing", "Pending", "pending"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": elem},
    }
    if orders_col.find_one(q, {"_id": 1}):
        return True

    alt = {"phone": phone, "network_id": network_id, "amount": amount_key}
    if kind == "offer":
        alt["value_obj.id"] = bval
    else:
        alt["value_obj.volume"] = bval
    if service_id_raw:
        alt["serviceId"] = service_id_raw
    q2 = {
        "status": {"$in": ["processing", "Pending", "pending"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": alt},
    }
    return bool(orders_col.find_one(q2, {"_id": 1}))

def _canonical_store_total_for_offer(
    store_doc: Dict[str, Any],
    svc_doc: Dict[str, Any],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    if not svc_doc:
        return None

    _percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    svc_id_str = str(svc_doc.get("_id_str") or svc_doc.get("_id") or "")
    per_entry = per_map.get(svc_id_str, {})

    offers = _svc_offers_list(svc_doc)
    if not offers:
        return None

    if svc_id_str == RESULTS_CHECKER_SERVICE_ID:
        checker_type = _results_checker_type(value_obj, value_raw)
        for idx, of in enumerate(offers):
            if _results_checker_type(of.get("value"), of.get("value_text")) != checker_type:
                continue
            base_amount = _offer_base_amount(of)
            if base_amount is None:
                return None
            offer_overrides = per_entry.get("offers") or {}
            if idx in offer_overrides:
                return round(float(offer_overrides[idx]), 2)
            return round(float(base_amount), 2)
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
    if base_amount is None:
        return None

    offer_overrides = per_entry.get("offers") or {}
    if best_idx in offer_overrides:
        return round(float(offer_overrides[best_idx]), 2)

    return round(float(base_amount), 2)

def _store_profit_percent_for_item(
    store_doc: Dict[str, Any],
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    value_raw: Any,
    base_amount: float,
) -> float:
    _percent_default, per_map = _build_pricing_map(store_doc.get("pricing") or {})
    if not svc_doc:
        return 0.0

    svc_id_str = str(svc_doc.get("_id_str") or svc_doc.get("_id") or "")
    per_entry = per_map.get(svc_id_str, {})

    offer_overrides = per_entry.get("offers") or {}
    if offer_overrides:
        offers = _svc_offers_list(svc_doc)
        if svc_id_str == RESULTS_CHECKER_SERVICE_ID:
            checker_type = _results_checker_type(value_obj, value_raw)
            for idx, of in enumerate(offers):
                if _results_checker_type(of.get("value"), of.get("value_text")) != checker_type:
                    continue
                override_total = _to_float(offer_overrides.get(idx))
                base = float(_offer_base_amount(of) or base_amount or 0.0)
                if override_total is not None and base > 0:
                    return round(((float(override_total) - base) / base) * 100.0, 2)
            return 0.0
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

        if best_idx is not None and best_idx in offer_overrides:
            override_total = _to_float(offer_overrides.get(best_idx))
            base = float(base_amount or 0.0)
            if base <= 0 and best_idx < len(offers):
                base = float(_offer_base_amount(offers[best_idx]) or 0.0)
            if override_total is not None and base > 0:
                return round(((float(override_total) - base) / base) * 100.0, 2)

    return 0.0

def _server_reprice_store_cart(
    store_doc: Dict[str, Any], cart: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], float]:
    revised: List[Dict[str, Any]] = []
    sys_total = 0.0
    for item in cart:
        service_id_raw = item.get("serviceId")
        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

        svc_doc: Optional[Dict[str, Any]] = None
        if service_id_raw:
            if str(service_id_raw).strip() == RESULTS_CHECKER_SERVICE_ID:
                svc_doc = _build_results_checker_service()
            try:
                if not svc_doc:
                    svc_doc = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {
                            "offers": 1,
                            "store_offers": 1,
                            "unit": 1,
                            "name": 1,
                            "type": 1,
                            "service_category": 1,
                                "provider": 1,
                            "default_profit_percent": 1,
                            "store_offers_profit": 1,
                            "status": 1,
                            "availability": 1,
                            "display": 1,
                            "network_id": 1,
                            "network": 1,
                        },
                    )
            except Exception:
                svc_doc = None

        is_unavail, reason_text = _service_unavailability_reason(svc_doc)
        if is_unavail:
            service_name = (
                (svc_doc or {}).get("name")
                or item.get("serviceName")
                or item.get("name")
                or "This package"
            )
            raise ValueError(f"{service_name}: {reason_text}")

        canonical = _canonical_store_total_for_offer(
            store_doc or {}, svc_doc or {}, value_obj, item.get("value")
        )
        if canonical is None:
            canonical = _money(item.get("amount"))

        revised_item = {**item, "amount": canonical}
        if svc_doc and _service_requires_known_number_verification(revised_item, svc_doc):
            revised_item["serviceName"] = revised_item.get("serviceName") or svc_doc.get("name")
            revised_item["service_network"] = revised_item.get("service_network") or svc_doc.get("service_network")
            revised_item["network"] = revised_item.get("network") or svc_doc.get("network")
        revised.append(revised_item)
        sys_total += canonical

    known_number_error = _known_number_validation_error(revised, source="store")
    if known_number_error:
        raise ValueError(str(known_number_error.get("message") or "Number verification failed."))

    return revised, round(sys_total, 2)


@stores_bp.route("/api/store-cart-validate/<slug>", methods=["POST"])
def validate_store_cart(slug: str):
    redir = _store_host_redirect()
    if redir:
        return redir

    try:
        body = request.get_json(silent=True) or {}
        cart = body.get("cart") or []
        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        try:
            revised, total_requested = _server_reprice_store_cart(store_doc, cart)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400

        return jsonify(
            {
                "success": True,
                "cart": revised,
                "total_amount": round(total_requested, 2),
            }
        ), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500

def _resolve_network_id(item: dict, value_obj: dict, svc_doc: dict | None):
    nid = (item or {}).get("network_id") or (value_obj or {}).get("network_id")
    if nid not in (None, "", []):
        try:
            return int(nid)
        except Exception:
            pass
    if svc_doc:
        try:
            if "network_id" in svc_doc and svc_doc["network_id"] not in (None, ""):
                return int(svc_doc["network_id"])
            guess = (svc_doc.get("name") or svc_doc.get("network") or "").strip().upper()
            if guess and guess in NETWORK_ID_FALLBACK:
                return int(NETWORK_ID_FALLBACK[guess])
        except Exception:
            pass
    if not svc_doc:
        name = (item.get("serviceName") or "").strip().upper()
        if name in NETWORK_ID_FALLBACK:
            return int(NETWORK_ID_FALLBACK[name])
    return None


# =====================================================================
# ✅ IMPORTANT FIX: Profit MUST be computed from SYSTEM offers (svc.offers)
# - base_amount = svc.offers[].amount
# - profit% = svc.store_offers_profit (fallback default_profit_percent)
# - profit = base_amount * profit%
# =====================================================================
def _system_offer_base_amount_from_service(
    svc_doc: Optional[Dict[str, Any]],
    value_obj: Any,
    value_raw: Any,
) -> Optional[float]:
    """
    ✅ System base amount must come from svc_doc.offers (NOT store_offers).
    We match closest offer by volume/minutes.
    """
    if not svc_doc:
        return None

    offers = svc_doc.get("offers")
    if not isinstance(offers, list) or not offers:
        return None

    svc_id_str = str(svc_doc.get("_id_str") or svc_doc.get("_id") or "")
    if svc_id_str == RESULTS_CHECKER_SERVICE_ID:
        checker_type = _results_checker_type(value_obj, value_raw)
        for of in offers:
            if _results_checker_type(of.get("value"), of.get("value_text")) == checker_type:
                return _to_float((of or {}).get("amount"))
        return None

    unit = _service_unit(svc_doc)
    vol_needed = _extract_volume(value_obj if isinstance(value_obj, dict) else value_raw, unit)

    best_idx: Optional[int] = None
    best_diff = float("inf")

    for idx, of in enumerate(offers):
        try:
            parsed = _parse_value_field(of.get("value"))
            vol = _extract_volume(parsed, unit)
            if vol_needed is not None and vol is not None:
                diff = abs(float(vol) - float(vol_needed))
                if diff < best_diff:
                    best_idx, best_diff = idx, diff
            elif best_idx is None:
                best_idx = idx
        except Exception:
            continue

    if best_idx is None:
        return None

    return _to_float((offers[best_idx] or {}).get("amount"))


def _deliver_results_checker(
    *,
    checker_type: str,
    phone: str,
    amount: float,
    order_id: str,
    line_index: int,
    user_id: Any,
    store_slug: str,
) -> Tuple[bool, Dict[str, Any]]:
    checker_type = (checker_type or "").strip().lower()
    if checker_type not in {"wassce", "bece"}:
        return False, {"message": "Invalid results checker type selected."}

    sold_doc = wassce_col.find_one_and_update(
        {"type": checker_type, "status": "not_sold"},
        {
            "$set": {
                "status": "sold",
                "sold_to": str(user_id) if user_id else "",
                "sold_at": datetime.utcnow(),
                "order_id": order_id,
                "line_index": int(line_index),
                "store_slug": store_slug,
                "sold_via": "store_page",
            }
        },
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not sold_doc:
        return False, {"message": f"No {checker_type.upper()} checker is currently available."}

    sms_phone = _normalize_arkesel_phone(phone)
    if not sms_phone:
        wassce_col.update_one(
            {"_id": sold_doc["_id"]},
            {"$set": {"status": "not_sold"}, "$unset": {"sold_to": "", "sold_at": "", "order_id": "", "line_index": "", "store_slug": "", "sold_via": ""}},
        )
        return False, {"message": "Invalid recipient phone for SMS delivery."}

    sms_text = str(sold_doc.get("message") or "").strip()
    sms_status = _send_arkesel_sms(sms_phone, sms_text)
    if sms_status != "sent":
        wassce_col.update_one(
            {"_id": sold_doc["_id"]},
            {"$set": {"status": "not_sold"}, "$unset": {"sold_to": "", "sold_at": "", "order_id": "", "line_index": "", "store_slug": "", "sold_via": ""}},
        )
        return False, {"message": "Could not send the results checker SMS right now. Please try again."}

    if user_id:
        try:
            purchase_history_col.insert_one(
                {
                    "user_id": str(user_id),
                    "checker_id": str(sold_doc["_id"]),
                    "type": checker_type,
                    "amount": round(float(amount or 0.0), 2),
                    "message": sms_text,
                    "source": "store_page",
                    "purchased_at": datetime.utcnow(),
                }
            )
        except Exception:
            pass

    return True, {
        "checker_id": str(sold_doc["_id"]),
        "sms_status": sms_status,
        "message": sms_text,
        "type": checker_type.upper(),
    }


def place_store_order(payload: Dict[str, Any], channel: str = "store_web") -> Tuple[Dict[str, Any], int]:
    store_doc = payload.get("store_doc") or {}
    slug = (payload.get("slug") or store_doc.get("slug") or "").strip()
    cart = payload.get("cart") or []
    payment_status = (payload.get("payment_status") or "unpaid").strip().lower()
    paid_from = (payload.get("paid_from") or "none").strip()
    source = payload.get("source") or {}
    user_id = payload.get("user_id")
    defer_provider_processing = bool(payload.get("defer_provider_processing"))
    order_status = (payload.get("order_status") or ("awaiting_payment" if payment_status == "pending" else "pending")).strip()
    charged_amount_override = round(float(_to_float(payload.get("charged_amount")) or 0.0), 2)
    gateway_fee_overage_ghs = round(float(_to_float(payload.get("gateway_fee_overage_ghs")) or 0.0), 2)

    if not slug or not store_doc:
        return {"success": False, "message": "Store not found"}, 404
    if not cart or not isinstance(cart, list):
        return {"success": False, "message": "Cart is empty or invalid"}, 400

    cart, total_requested = _server_reprice_store_cart(store_doc, cart)
    if total_requested <= 0:
        return {"success": False, "message": "Total amount must be greater than zero"}, 400

    order_id = (payload.get("order_id") or generate_order_id()).strip()
    results: List[Dict[str, Any]] = []
    api_jobs: List[Dict[str, Any]] = []
    profit_amount_total = 0.0
    total_processing_amount = 0.0

    for idx, item in enumerate(cart, start=1):
        phone = (item.get("phone") or "").strip()
        amt_total = _money(item.get("amount"))
        service_id_raw = item.get("serviceId")
        svc_doc: Optional[Dict[str, Any]] = None
        svc_name = item.get("serviceName") or None
        svc_type = None

        if service_id_raw:
            if str(service_id_raw).strip() == RESULTS_CHECKER_SERVICE_ID:
                svc_doc = _build_results_checker_service()
            try:
                if not svc_doc:
                    svc_doc = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {
                            "type": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "offers": 1,
                            "store_offers": 1,
                            "store_offers_profit": 1,
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "provider": 1,
                            "status": 1,
                            "availability": 1,
                            "display": 1,
                            "unit": 1,
                        },
                    )
                if svc_doc:
                    raw_type = svc_doc.get("type")
                    svc_type = raw_type.strip().upper() if isinstance(raw_type, str) else raw_type
                    svc_name = svc_doc.get("name") or svc_doc.get("network") or svc_name
            except Exception:
                svc_doc = None

        is_unavail, reason_text = _service_unavailability_reason(svc_doc)
        if is_unavail:
            return {
                "success": False,
                "message": reason_text,
                "unavailable": {
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "reason": reason_text,
                },
            }, 400

        value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))
        base_amount = round(float(_to_float(item.get("base_amount")) or 0.0), 2)
        system_offer_base = _system_offer_base_amount_from_service(svc_doc, value_obj, item.get("value"))
        profit_amount = 0.0
        profit_percent_used = 0.0
        if system_offer_base is not None and base_amount > 0:
            profit_amount = max(0.0, round(base_amount - float(system_offer_base), 2))
            if system_offer_base > 0:
                profit_percent_used = round((profit_amount / float(system_offer_base)) * 100.0, 2)
        profit_amount_total += profit_amount

        network_id = _resolve_network_id(item, value_obj, svc_doc) if svc_doc else None
        store_profit_amount = max(0.0, round(amt_total - base_amount, 2))
        external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"
        resolved_network = _resolve_network_slug(svc_doc, item)
        package_size_gb = _resolve_package_size_gb(value_obj, item)

        svc_name_norm = (svc_name or "").strip().lower()
        svc_network_norm = (svc_doc.get("network") or "").strip().lower() if svc_doc else ""
        combo_name_net = f"{svc_name_norm} {svc_network_norm}"
        is_mtn_express = svc_name_norm == "mtn express"
        is_mtn_normal = svc_name_norm == "mtn normal"
        is_telecel_bundle = "telecel" in combo_name_net
        is_ishare_bundle = (
            "ishare" in combo_name_net
            or "i share" in combo_name_net
            or "at - ishare" in combo_name_net
        )

        svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
        api_allowed = svc_type_flag in ("ON", "API") or is_telecel_bundle or is_ishare_bundle
        if svc_type_flag == "OFF":
            api_allowed = False

        provider_raw = svc_doc.get("provider") if svc_doc else None
        svc_provider = str(provider_raw or "").strip().lower()
        if svc_provider not in ("portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"):
            svc_provider = "portal02"
        if svc_provider in ("dataconnect", "datakazina") and not (is_mtn_express or is_mtn_normal):
            svc_provider = "portal02"

        provider_name = "manual"
        provider_network = resolved_network
        api_status = "manual_pending"
        api_note = "Queued for manual processing."
        job_payload: Optional[Dict[str, Any]] = None

        if str(service_id_raw).strip() == RESULTS_CHECKER_SERVICE_ID:
            checker_type = _results_checker_type(value_obj, item.get("value"))
            ok_delivery, delivery = _deliver_results_checker(
                checker_type=checker_type,
                phone=phone,
                amount=amt_total,
                order_id=order_id,
                line_index=idx,
                user_id=user_id,
                store_slug=slug,
            )
            if not ok_delivery:
                return {"success": False, "message": delivery.get("message") or "Results checker delivery failed."}, 400

            line_record = {
                "phone": phone,
                "base_amount": base_amount,
                "amount": amt_total,
                "profit_amount": profit_amount,
                "profit_percent_used": profit_percent_used,
                "store_profit_amount": store_profit_amount,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name or RESULTS_CHECKER_NAME,
                "service_type": "MANUAL",
                "provider": "arkesel_sms",
                "provider_network": "sms",
                "provider_reference": str(delivery.get("checker_id") or ""),
                "provider_order_id": None,
                "provider_request_order_id": external_ref,
                "network_id": None,
                "line_status": "delivered",
                "api_status": "sms_sent",
                "api_response": {
                    "note": f"{delivery.get('type') or checker_type.upper()} results checker sent by SMS.",
                    "checker_id": delivery.get("checker_id"),
                    "sms_status": delivery.get("sms_status"),
                },
            }
            results.append(line_record)
            total_processing_amount += amt_total
            continue

        if api_allowed and svc_provider == "codecraft":
            network = _resolve_codecraft_network(svc_doc, item)
            gig = _resolve_codecraft_gig(value_obj, item)
            package_map = _codecraft_get_packages_cached()
            provider_amount = package_map.get((network, gig)) if network and gig else None
            provider_name = "codecraft"
            provider_network = network
            if phone and network and gig and provider_amount is not None:
                api_status = "submitting"
                api_note = "Submitting directly to CodeCraft."
                job_payload = {
                    "provider_request_order_id": external_ref, "phone": phone, "amount": amt_total,
                    "provider": "codecraft", "provider_network": network, "provider_gig": gig,
                    "provider_amount": provider_amount,
                    "service_id": svc_doc["_id"] if svc_doc else None, "line_index": idx,
                }
            else:
                api_status = "skipped_package_not_found" if network and gig else "skipped_missing_fields"
                api_note = "CodeCraft fields or package missing; queued for manual processing."

        elif api_allowed and svc_provider == "bundleportal":
            provider_name = "bundleportal"
            provider_network = resolved_network if resolved_network in ("mtn", "telecel", "airteltigo") else None
            if phone and provider_network and package_size_gb is not None:
                api_status = "submitting"
                api_note = "Submitting directly to BundlePortal."
                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "amount": amt_total,
                    "provider": "bundleportal",
                    "provider_network": provider_network,
                    "package_size_gb": package_size_gb,
                    "service_id": svc_doc["_id"] if svc_doc else None,
                    "line_index": idx,
                }
            else:
                api_status = "skipped_missing_fields"
                api_note = "BundlePortal fields missing; queued for manual processing."

        elif api_allowed and svc_provider == "datakazina":
            shared_bundle = _resolve_datakazina_shared_bundle(value_obj, item)
            provider_name = "datakazina"
            provider_network = "mtn"
            if phone and shared_bundle:
                api_status = "submitting"
                api_note = "Submitting directly to DataKazina."
                network_id = 3
                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "datakazina",
                    "network_id": 3,
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"] if svc_doc else None,
                    "line_index": idx,
                }
            else:
                api_status = "skipped_missing_fields"
                api_note = "DataKazina fields missing; queued for manual processing."

        elif api_allowed and svc_provider == "dataconnect":
            shared_bundle = _resolve_shared_bundle_mb(value_obj, item)
            dc_network_id = _resolve_network_id(item, value_obj, svc_doc)
            if dc_network_id is None and resolved_network == "mtn":
                dc_network_id = 3
            provider_name = "dataconnect"
            provider_network = "mtn"
            if phone and shared_bundle and dc_network_id:
                api_status = "submitting"
                api_note = "Submitting directly to DataConnect."
                network_id = int(dc_network_id)
                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "dataconnect",
                    "network_id": int(dc_network_id),
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"] if svc_doc else None,
                    "line_index": idx,
                }
            else:
                api_status = "skipped_missing_fields"
                api_note = "DataConnect fields missing; queued for manual processing."

        elif api_allowed and svc_provider == "skplug":
            skplug_network = _resolve_skplug_network(svc_doc, item)
            provider_name = "skplug"
            provider_network = skplug_network
            if phone and skplug_network and package_size_gb is not None:
                api_status = "submitting"
                api_note = "Submitting directly to SKPlug."
                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "skplug",
                    "provider_network": skplug_network,
                    "package_size_gb": package_size_gb,
                    "service_id": svc_doc["_id"] if svc_doc else None,
                    "line_index": idx,
                }
            else:
                api_status = "skipped_missing_fields"
                api_note = "SKPlug fields missing; queued for manual processing."

        elif api_allowed and svc_provider == "portal02" and package_size_gb is not None and resolved_network in ("mtn", "telecel", "airteltigo"):
            provider_name = "portal02"
            provider_network = resolved_network
            api_status = "submitting"
            api_note = "Submitting directly to Portal-02."
            job_payload = {
                "provider_request_order_id": external_ref,
                "phone": phone,
                "provider": "portal02",
                "portal02_network_slug": resolved_network,
                "package_size_gb": package_size_gb,
                "service_id": svc_doc["_id"] if svc_doc else None,
                "raw_item": item,
            }

        line_record = {
            "phone": phone,
            "base_amount": base_amount,
            "amount": amt_total,
            "profit_amount": profit_amount,
            "profit_percent_used": profit_percent_used,
            "store_profit_amount": store_profit_amount,
            "value": item.get("value"),
            "value_obj": value_obj,
            "serviceId": service_id_raw,
            "serviceName": svc_name,
            "service_type": svc_type,
            "provider": provider_name,
            "provider_network": provider_network,
            "provider_reference": None,
            "provider_order_id": None,
            "provider_request_order_id": external_ref,
            "network_id": network_id,
            "line_status": "processing",
            "api_status": api_status,
            "api_response": {
                "note": api_note,
                "configured_provider": svc_provider,
                "resolved_network": resolved_network,
            },
        }
        results.append(line_record)
        total_processing_amount += amt_total

        if job_payload:
            api_jobs.append(job_payload)

    now = datetime.utcnow()
    if results and all(str(it.get("line_status") or "").lower() == "delivered" for it in results):
        order_status = "delivered"
    effective_charged_amount = round(charged_amount_override if charged_amount_override > 0 else total_processing_amount, 2)
    if defer_provider_processing:
        for it in results:
            if it.get("line_status") == "processing":
                it["line_status"] = "awaiting_payment"
            if it.get("api_status") == "submitting":
                it["api_status"] = "payment_pending"
            note = it.get("api_response") if isinstance(it.get("api_response"), dict) else {}
            note["note"] = "Awaiting Paystack mobile money payment before provider processing."
            it["api_response"] = note

    order_doc = {
        "user_id": user_id,
        "store_slug": slug,
        "order_id": order_id,
        "items": results,
        "total_amount": round(total_requested, 2),
        "charged_amount": effective_charged_amount,
        "profit_amount_total": round(profit_amount_total, 2),
        "status": order_status,
        "payment_status": payment_status,
        "paid_from": paid_from,
        "channel": channel,
        "source": source,
        "created_at": now,
        "updated_at": now,
        "debug": {
            "store_checkout": True,
            "shared_place_store_order": True,
            "channel": channel,
            "gateway_fee_overage_ghs": gateway_fee_overage_ghs,
        },
    }
    if defer_provider_processing:
        order_doc["pending_provider_jobs"] = api_jobs
        order_doc["provider_processing_started"] = False
    for key in ("payment_provider", "payment_channel", "payment_reference", "payment_access_code", "paystack_reference"):
        if payload.get(key):
            order_doc[key] = payload.get(key)

    created_docs, order_jobs = _persist_store_split_orders(
        base_order_fields=order_doc,
        results=results,
        api_jobs=api_jobs,
    )
    try:
        _credit_paid_store_order_profits(slug, created_docs, payment_status)
    except Exception as exc:
        jlog("store_account_update_error", store_slug=slug, error=str(exc))
    primary_order_doc = created_docs[0] if created_docs else order_doc
    primary_order_id = primary_order_doc.get("order_id") or order_id

    tx_doc = {
        "user_id": user_id,
        "amount": effective_charged_amount,
        "reference": primary_order_id,
        "status": "success" if payment_status == "paid" else "pending",
        "payment_status": payment_status,
        "type": "debit",
        "source": channel,
        "channel": channel,
        "currency": "GHS",
        "created_at": now,
        "updated_at": now,
        "meta": {
            "store_checkout": True,
            "store_slug": slug,
            "order_id": primary_order_id,
            "source": source,
        },
    }
    transactions_col.insert_one(tx_doc)

    if order_jobs and not defer_provider_processing:
        # Store checkout must finish each provider submission before responding.
        # A daemon thread can disappear when a Gunicorn/Render worker exits and
        # leave an already-paid order permanently marked as queued.
        for split_order_id, split_jobs in order_jobs:
            _background_process_providers(split_order_id, split_jobs)

        refreshed_docs = [orders_col.find_one({"order_id": doc.get("order_id")}) or doc for doc in created_docs]
        created_docs = refreshed_docs
        primary_order_doc = refreshed_docs[0] if refreshed_docs else primary_order_doc
        results = [item for doc in refreshed_docs for item in (doc.get("items") or [])]
        order_status = str(primary_order_doc.get("status") or order_status)

    response_status = order_status
    response_message = (
        f"Results checker sent successfully. Order ID: {primary_order_id}"
        if response_status == "delivered"
        else f"Order received. Order ID: {primary_order_id}"
    )

    return {
        "success": True,
        "message": response_message,
        "order_id": primary_order_id,
        "order_ids": [doc.get("order_id") for doc in created_docs],
        "status": order_status,
        "payment_status": payment_status,
        "charged_amount": effective_charged_amount,
        "profit_amount_total": round(profit_amount_total, 2),
        "items": results,
    }, 200


@stores_bp.route("/store-checkout/<slug>", methods=["POST"])
def store_checkout_paystack(slug: str):
    redir = _store_host_redirect()
    if redir:
        return redir
    try:
        body = request.get_json(silent=True) or {}
        cart = body.get("cart") or []
        method = (body.get("method") or "paystack_inline").strip().lower()
        ps_info = body.get("paystack") or {}
        ps_ref = (ps_info.get("reference") or "").strip()
        paystack_verified = False

        jlog("store_public_checkout_incoming", slug=slug, payload={"method": method, "has_ref": bool(ps_ref), "cart_len": len(cart) if isinstance(cart, list) else -1})

        store_doc = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store_doc:
            return jsonify({"success": False, "message": "Store not found"}), 404

        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400

        # idempotency: same reference should not create multiple orders
        if ps_ref:
            prior = orders_col.find_one({"store_slug": slug, "paystack_reference": ps_ref})
            if prior:
                return jsonify(
                    {
                        "success": True,
                        "message": f"✅ Order already created. Order ID: {prior.get('order_id')}",
                        "order_id": prior.get("order_id"),
                        "status": prior.get("status"),
                        "charged_amount": prior.get("charged_amount"),
                        "profit_amount_total": prior.get("profit_amount_total", 0.0),
                        "items": prior.get("items", []),
                        "idempotent": True,
                    }
                ), 200

        # server-side repricing (prevents client tampering)
        try:
            cart, total_requested = _server_reprice_store_cart(store_doc, cart)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        if total_requested <= 0:
            return jsonify({"success": False, "message": "Total amount must be greater than zero"}), 400

        if method != "paystack_inline" or not ps_ref:
            return jsonify({"success": False, "message": "Payment missing. Please pay first."}), 400

        ok, verify_data, fail_reason = _verify_paystack(ps_ref)
        if not ok:
            return jsonify({"success": False, "message": f"Payment verification failed: {fail_reason}"}), 400
        paystack_verified = True

        paid_pes = int(verify_data.get("amount") or 0)
        paid_ghs = round(paid_pes / 100.0, 2)
        currency = (verify_data.get("currency") or "GHS").upper()
        if paid_pes <= 0 or currency != "GHS":
            return jsonify({"success": False, "message": "Invalid payment amount/currency."}), 400

        expected_pay_ghs = round(total_requested, 2)
        expected_pay_pes = int(round(expected_pay_ghs * 100))
        if not _paid_enough(paid_pes, expected_pay_pes):
            jlog(
                "store_public_checkout_amount_underpaid",
                slug=slug,
                paid_pes=paid_pes,
                expected_pes=expected_pay_pes,
                paid_ghs=paid_ghs,
                expected_ghs=expected_pay_ghs,
            )
            return jsonify(
                {
                    "success": False,
                    "message": "Payment amount is less than required. Please complete full payment.",
                    "paid": paid_ghs,
                    "required": expected_pay_ghs,
                }
            ), 400

        fee_delta_ghs = max(0.0, round(paid_ghs - expected_pay_ghs, 2))

        # Keep all store order creation/provider processing in the shared path.
        # The legacy implementation below this return drifted from that path and
        # referenced split-order variables that it never created, turning a
        # successful payment into a generic 500 response after the order insert.
        result, status_code = place_store_order(
            {
                "store_doc": store_doc,
                "slug": slug,
                "cart": cart,
                "payment_status": "paid",
                "paid_from": "paystack_inline",
                "payment_provider": "paystack",
                "payment_channel": verify_data.get("channel"),
                "payment_reference": ps_ref,
                "paystack_reference": ps_ref,
                "charged_amount": expected_pay_ghs,
                "gateway_fee_overage_ghs": fee_delta_ghs,
                "user_id": ObjectId(session["user_id"]) if session.get("user_id") else None,
                "source": {
                    "store_checkout": True,
                    "paystack_paid_ghs": paid_ghs,
                    "paystack_expected_ghs": expected_pay_ghs,
                },
            },
            channel="store_checkout",
        )
        if result.get("success"):
            result["paid_ghs"] = paid_ghs
            result["expected_ghs"] = expected_pay_ghs
        return jsonify(result), status_code

        # transaction doc (align with checkout.py)
        txn_user_id = ObjectId(session["user_id"]) if session.get("user_id") else None
        txn_doc = {
            "user_id": txn_user_id,
            "amount": round(paid_ghs, 2),
            "reference": ps_ref,
            "status": "success",
            "type": "debit",
            "source": "paystack_inline",
            "gateway": "Paystack",
            "currency": "GHS",
            "channel": verify_data.get("channel"),
            "verified_at": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "raw": verify_data,
            "meta": {
                "store_checkout": True,
                "store_slug": slug,
                "expected_pay_total_ghs": expected_pay_ghs,
                "paid_total_ghs": paid_ghs,
                "gateway_fee_overage_ghs": fee_delta_ghs,
                "note": "Customer payment captured via store inline checkout (server repriced).",
            },
        }

        if not transactions_col.find_one({"reference": ps_ref, "source": "paystack_inline", "status": "success"}):
            if _checkout_helpers.get("txn_fn"):
                try:
                    _checkout_helpers["txn_fn"](transactions_col, txn_doc)
                except Exception:
                    transactions_col.insert_one(txn_doc)
            else:
                transactions_col.insert_one(txn_doc)

        order_id = generate_order_id()
        results: List[Dict[str, Any]] = []
        debug_events: List[Dict[str, Any]] = []

        profit_amount_total = 0.0
        total_processing_amount = 0.0
        seen_keys = set()
        api_jobs: List[Dict[str, Any]] = []

        for idx, item in enumerate(cart, start=1):
            phone = (item.get("phone") or "").strip()
            amt_total = _money(item.get("amount"))
            amount_key = _normalize_amount_key(amt_total)

            service_id_raw = item.get("serviceId")
            svc_doc: Optional[Dict[str, Any]] = None
            svc_type: Optional[str] = None
            svc_name = item.get("serviceName") or None

            if service_id_raw:
                if str(service_id_raw).strip() == RESULTS_CHECKER_SERVICE_ID:
                    svc_doc = _build_results_checker_service()
                try:
                    if not svc_doc:
                        svc_doc = services_col.find_one(
                            {"_id": ObjectId(service_id_raw)},
                        {
                            "type": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "offers": 1,
                            "store_offers": 1,
                            "store_offers_profit": 1,   # ✅ needed
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "provider": 1,
                            "status": 1,
                            "availability": 1,
                            "display": 1,
                            "unit": 1,
                            },
                        )
                    if svc_doc:
                        st = svc_doc.get("type")
                        svc_type = st.strip().upper() if isinstance(st, str) else st
                        svc_name = svc_doc.get("name") or svc_doc.get("network") or svc_name
                except Exception:
                    svc_doc = None
                    svc_type = None

            is_unavail, reason_text = _service_unavailability_reason(svc_doc)
            if is_unavail:
                return jsonify(
                    {
                        "success": False,
                        "message": reason_text,
                        "unavailable": {"serviceId": service_id_raw, "serviceName": svc_name, "reason": reason_text},
                    }
                ), 400

            value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))

            # -----------------------------------------------------------------
            # Profit logic (requested):
            # profit_amount = store base price - system offer base (svc.offers)
            # -----------------------------------------------------------------
            system_offer_base = _system_offer_base_amount_from_service(svc_doc, value_obj, item.get("value"))
            base_amount = round(float(_to_float(item.get("base_amount")) or 0.0), 2)
            profit_amount = 0.0
            profit_percent_used = 0.0
            if system_offer_base is not None and base_amount > 0:
                profit_amount = max(0.0, round(base_amount - float(system_offer_base), 2))
                if system_offer_base > 0:
                    profit_percent_used = round((profit_amount / float(system_offer_base)) * 100.0, 2)
            profit_amount_total += profit_amount
            store_profit_percent = _store_profit_percent_for_item(
                store_doc, svc_doc, value_obj, item.get("value"), base_amount
            )
            store_profit_amount = max(0.0, round(amt_total - base_amount, 2))
            store_profit_field = {"store_profit_amount": store_profit_amount} if paystack_verified else {}

            if str(service_id_raw).strip() == RESULTS_CHECKER_SERVICE_ID:
                checker_type = _results_checker_type(value_obj, item.get("value"))
                ok_delivery, delivery = _deliver_results_checker(
                    checker_type=checker_type,
                    phone=phone,
                    amount=amt_total,
                    order_id=order_id,
                    line_index=idx,
                    user_id=(txn_user_id or session.get("user_id")),
                    store_slug=slug,
                )
                if not ok_delivery:
                    return jsonify({"success": False, "message": delivery.get("message") or "Results checker delivery failed."}), 400

                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name or RESULTS_CHECKER_NAME,
                        "service_type": "MANUAL",
                        "provider": "arkesel_sms",
                        "provider_network": "sms",
                        "provider_reference": str(delivery.get("checker_id") or ""),
                        "provider_order_id": None,
                        "provider_request_order_id": f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}",
                        "network_id": None,
                        "line_status": "delivered",
                        "api_status": "sms_sent",
                        "api_response": {
                            "note": f"{delivery.get('type') or checker_type.upper()} results checker sent by SMS.",
                            "checker_id": delivery.get("checker_id"),
                            "sms_status": delivery.get("sms_status"),
                        },
                    }
                )
                total_processing_amount += amt_total
                continue

            # network + api
            network_id = _resolve_network_id(item, value_obj, svc_doc) if svc_doc else None

            # bundle key (dedupe)
            shared_bundle_for_key = None
            if svc_doc:
                unit = _service_unit(svc_doc)
                vol_for_key = _extract_volume(
                    value_obj if isinstance(value_obj, dict) else item.get("value"), unit
                )
                if vol_for_key is not None:
                    try:
                        shared_bundle_for_key = int(vol_for_key)
                    except Exception:
                        shared_bundle_for_key = None

            is_express = False
            bundle_key = _build_bundle_key(is_express, shared_bundle_for_key, value_obj)

            if phone and (network_id is not None) and (bundle_key is not None):
                cart_key = (phone, int(network_id), int(bundle_key[1]), bundle_key[0], amount_key)
                if cart_key in seen_keys:
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": 0.0,
                            "amount": 0.0,
                            "originally_requested_amount": amt_total,
                            "profit_amount": 0.0,
                            "profit_percent_used": 0.0,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": (svc_type if svc_type else ("unknown" if not svc_doc else None)),
                            "network_id": network_id,
                            "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]},
                            "line_amount_key": amount_key,
                            "line_status": "skipped_duplicate_in_cart",
                            "api_status": "skipped",
                            "api_response": {"note": "Duplicate line in this cart (same number, network, bundle, amount)"},
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
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": (svc_type if svc_type else ("unknown" if not svc_doc else None)),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_processing",
                        "api_status": "skipped",
                        "api_response": {
                            "note": "Same number + same network + same bundle + same amount already processing; skipping."
                        },
                    }
                )
                continue

            resolved_network = _resolve_network_slug(svc_doc, item)
            svc_name_norm = (svc_name or "").strip().lower()
            svc_network_norm = (svc_doc.get("network") or "").strip().lower() if svc_doc else ""
            combo_name_net = f"{svc_name_norm} {svc_network_norm}"

            is_mtn_express = (svc_name_norm == "mtn express")
            is_mtn_normal = (svc_name_norm == "mtn normal")
            is_telecel_bundle = ("telecel" in combo_name_net)
            is_ishare_bundle = (
                "ishare" in combo_name_net
                or "i share" in combo_name_net
                or "at - ishare" in combo_name_net
            )

            svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
            type_allows_api = svc_type_flag in ("ON", "API")
            api_allowed = type_allows_api or is_telecel_bundle or is_ishare_bundle
            if svc_type_flag == "OFF":
                api_allowed = False

            provider_raw = svc_doc.get("provider") if svc_doc else None
            svc_provider_from_db = str(provider_raw).strip().lower() if provider_raw is not None else ""
            if svc_provider_from_db not in ("portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"):
                jlog(
                    "provider_defaulted_to_portal02",
                    order_id=order_id,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider_raw=provider_raw,
                    reason="missing_or_invalid",
                )
                svc_provider_from_db = "portal02"

            if svc_provider_from_db == "dataconnect" and not (is_mtn_express or is_mtn_normal):
                jlog(
                    "provider_defaulted_to_portal02",
                    order_id=order_id,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider_raw=provider_raw,
                    reason="non_mtn_service_for_dataconnect",
                )
                svc_provider_from_db = "portal02"

            if svc_provider_from_db == "datakazina" and not (is_mtn_express or is_mtn_normal):
                jlog(
                    "provider_defaulted_to_portal02",
                    order_id=order_id,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider_raw=provider_raw,
                    reason="non_mtn_service_for_datakazina",
                )
                svc_provider_from_db = "portal02"

            use_codecraft = svc_provider_from_db == "codecraft" and api_allowed
            use_datakazina = (
                (is_mtn_express or is_mtn_normal)
                and svc_provider_from_db == "datakazina"
                and api_allowed
            )
            use_dataconnect = (
                (is_mtn_express or is_mtn_normal)
                and svc_provider_from_db == "dataconnect"
                and api_allowed
            )
            use_skplug = api_allowed and svc_provider_from_db == "skplug"
            skplug_network = _resolve_skplug_network(svc_doc, item) if use_skplug else None
            use_bundleportal = api_allowed and svc_provider_from_db == "bundleportal"

            portal02_network_slug = None
            if api_allowed and svc_provider_from_db == "portal02":
                if resolved_network in ("mtn", "telecel", "airteltigo"):
                    portal02_network_slug = resolved_network

            use_portal02 = portal02_network_slug is not None

            jlog(
                "store_line_routing",
                order_id=order_id,
                idx=idx,
                serviceId=service_id_raw,
                serviceName=svc_name,
                resolved_network=resolved_network,
                api_allowed=api_allowed,
                svc_provider_from_db=svc_provider_from_db,
                datakazina_selected=use_datakazina,
                selected_provider=(
                    "codecraft" if use_codecraft else "bundleportal" if use_bundleportal else "datakazina" if use_datakazina else "dataconnect" if use_dataconnect else "skplug" if use_skplug else "portal02" if use_portal02 else "manual"
                ),
            )

            if use_codecraft:
                network = _resolve_codecraft_network(svc_doc, item)
                gig = _resolve_codecraft_gig(value_obj, item)
                package_map = _codecraft_get_packages_cached()
                provider_amount = package_map.get((network, gig)) if network and gig else None
                if not phone or not network or not gig or provider_amount is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append({
                        "phone": phone, "base_amount": base_amount, "amount": amt_total,
                        "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                        "store_profit_amount": store_profit_amount,
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
                api_requested_total += amt_total
                has_processing = True
                total_processing_amount += amt_total
                results.append({
                    "phone": phone, "base_amount": base_amount, "amount": amt_total,
                    "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                    "store_profit_amount": store_profit_amount,
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
                    "provider_amount": provider_amount, "service_id": svc_doc["_id"], "line_index": idx,
                })
                continue

            if use_bundleportal:
                package_size_gb = _resolve_package_size_gb(value_obj, item)
                bp_network = resolved_network if resolved_network in ("mtn", "telecel", "airteltigo") else None
                if not phone or not bp_network or package_size_gb is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append({
                        "phone": phone, "base_amount": base_amount, "amount": amt_total,
                        "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                        "store_profit_amount": store_profit_amount,
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
                api_requested_total += amt_total
                has_processing = True
                total_processing_amount += amt_total
                results.append({
                    "phone": phone, "base_amount": base_amount, "amount": amt_total,
                    "profit_amount": profit_amount, "profit_percent_used": profit_percent_used,
                    "store_profit_amount": store_profit_amount,
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
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **store_profit_field,
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
                            "api_response": {
                                "note": "DataKazina fields missing; queued for processing",
                                "got": {"phone": bool(phone), "shared_bundle": shared_bundle},
                            },
                            "provider": "datakazina",
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **store_profit_field,
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

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "datakazina",
                    "network_id": dk_network_id,
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
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
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **store_profit_field,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": dc_network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "DataConnect fields missing; queued for processing",
                                "got": {"phone": bool(phone), "network_id": dc_network_id, "shared_bundle": shared_bundle},
                            },
                            "provider": "dataconnect",
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **store_profit_field,
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
                    "network_id": int(dc_network_id),
                    "shared_bundle": int(shared_bundle),
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "processing",
                    "api_status": "submitting",
                    "api_response": {"note": "Submitting directly to DataConnect"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "dataconnect",
                    "network_id": int(dc_network_id),
                    "shared_bundle": int(shared_bundle),
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_skplug:
                package_size_gb = _resolve_package_size_gb(value_obj, item)

                if not phone or not skplug_network or package_size_gb is None:
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **store_profit_field,
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
                                "note": "SKPlug fields missing; queued for processing",
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

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **store_profit_field,
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

                results.append(line_record)

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

            if not use_portal02:
                total_processing_amount += amt_total

                if not api_allowed:
                    note = (
                        "API calls disabled for this service (type OFF and not a mapped Telecel/iShare); "
                        "queued for manual processing."
                    )
                    api_status = "not_applicable_type_off"
                else:
                    note = (
                        "API is handled via Portal-02, but this line did not match any mapped network; "
                        "queued for manual processing."
                    )
                    api_status = "not_applicable_network"

                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **store_profit_field,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": api_status,
                        "api_response": {
                            "note": note,
                            "resolved_network": resolved_network,
                            "serviceName": svc_name,
                            "service_type_flag": svc_type_flag,
                        },
                    }
                )
                continue

            package_size_gb = _resolve_package_size_gb(value_obj, item)

            if not phone or package_size_gb is None:
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **store_profit_field,
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
                            "note": "API fields missing; queued for processing",
                            "got": {
                                "phone": bool(phone),
                                "resolved_network": resolved_network,
                                "package_size_gb": package_size_gb,
                            },
                        },
                    }
                )
                continue

            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

            provider_name = "portal02"
            provider_network_slug = portal02_network_slug

            total_processing_amount += amt_total

            line_record = {
                "phone": phone,
                "base_amount": base_amount,
                "amount": amt_total,
                "profit_amount": profit_amount,
                "profit_percent_used": profit_percent_used,
                **store_profit_field,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name,
                "service_type": svc_type,
                "provider": provider_name,
                "provider_network": provider_network_slug,
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

            results.append(line_record)

            job_payload = {
                "provider_request_order_id": external_ref,
                "phone": phone,
                "provider": provider_name,
                "portal02_network_slug": portal02_network_slug,
                "package_size_gb": package_size_gb,
                "service_id": svc_doc["_id"] if svc_doc else None,
                "raw_item": item,
            }

            api_jobs.append(job_payload)

        skipped_count = sum(
            1
            for it in results
            if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
        )

        store_profit_total = 0.0
        if paystack_verified:
            store_profit_total = sum(_money(it.get("store_profit_amount")) for it in results)
        final_status = "delivered" if results and all(str(it.get("line_status") or "").lower() == "delivered" for it in results) else "pending"

        order_doc = {
            "user_id": (ObjectId(session["user_id"]) if session.get("user_id") else None),
            "store_slug": slug,
            "order_id": order_id,
            "items": results,
            "total_amount": round(total_requested, 2),
            "charged_amount": round(total_processing_amount, 2),
            "profit_amount_total": round(profit_amount_total, 2),
            "status": final_status,
            "paid_from": "paystack_inline",
            "paystack_reference": ps_ref,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "debug": {
                "store_checkout": True,
                "events": debug_events[-10:],
                "paystack_paid_ghs": paid_ghs,
                "paystack_expected_ghs": expected_pay_ghs,
                "gateway_fee_overage_ghs": fee_delta_ghs,
                "skipped_count": skipped_count,
            },
        }

        for it in (order_doc.get("items") or []):
            if not it.get("line_status"):
                it["line_status"] = "pending"
            if isinstance(it.get("value"), (dict, list)):
                it["value"] = ""
            if not it.get("value"):
                vo = it.get("value_obj") or {}
                vol = vo.get("volume")
                if isinstance(vol, (int, float)) and vol > 0:
                    gb = vol / 1000
                    it["value"] = f"{gb:g}GB"
                else:
                    it["value"] = "N/A"

        if _checkout_helpers.get("order_fn"):
            try:
                _checkout_helpers["order_fn"](orders_col, order_doc)
            except Exception:
                orders_col.insert_one(order_doc)
        else:
            orders_col.insert_one(order_doc)

        if paystack_verified and store_profit_total > 0:
            try:
                store_accounts_col.update_one(
                    {"store_slug": slug},
                    {
                        "$inc": {"total_profit_balance": round(store_profit_total, 2)},
                        "$set": {
                            "last_updated_profit": round(store_profit_total, 2),
                            "updated_at": datetime.utcnow(),
                        },
                        "$setOnInsert": {
                            "store_slug": slug,
                            "created_at": datetime.utcnow(),
                        },
                    },
                    upsert=True,
                )
            except Exception:
                jlog("store_account_update_error", store_slug=slug)

        if order_jobs:
            # Submit synchronously so the provider result is stored before the
            # store checkout response is returned. This prevents paid orders
            # from being stranded when a daemon thread is terminated.
            for split_order_id, split_jobs in order_jobs:
                _background_process_providers(split_order_id, split_jobs)

            refreshed_docs = [orders_col.find_one({"order_id": doc.get("order_id")}) or doc for doc in created_docs]
            created_docs = refreshed_docs
            primary_order_doc = refreshed_docs[0] if refreshed_docs else primary_order_doc
            results = [item for doc in refreshed_docs for item in (doc.get("items") or [])]
            final_status = str(primary_order_doc.get("status") or final_status)

        response_status = "delivered" if final_status == "delivered" else "processing"
        response_message = (
            f"✅ Results checker sent successfully. Order ID: {primary_order_id}"
            if final_status == "delivered"
            else f"✅ Order received and is processing. Order ID: {primary_order_id}"
        )

        return jsonify(
            {
                "success": True,
                "message": response_message,
                "order_id": primary_order_id,
                "order_ids": [doc.get("order_id") for doc in created_docs],
                "status": response_status,
                "charged_amount": round(total_processing_amount, 2),
                "profit_amount_total": round(profit_amount_total, 2),
                "skipped_count": skipped_count,
                "items": results,
                "paid_ghs": paid_ghs,
                "expected_ghs": expected_pay_ghs,
            }
        ), 200

    except Exception:
        try:
            jlog("store_public_checkout_uncaught", slug=slug, error=traceback.format_exc())
        except Exception:
            pass
        return jsonify({"success": False, "message": "Server error"}), 500


# ---------------------------------------------------------------------
# Optional helper endpoint (safe): fetch order summary by order_id
# (does not expose sensitive paystack raw)
# ---------------------------------------------------------------------
@stores_bp.route("/api/store-order/<order_id>", methods=["GET"])
def api_store_order(order_id: str):
    try:
        order_id = (order_id or "").strip()
        if not order_id:
            return jsonify({"success": False, "message": "order_id required"}), 400

        doc = orders_col.find_one(
            {"order_id": order_id},
            {
                "_id": 0,
                "order_id": 1,
                "store_slug": 1,
                "status": 1,
                "total_amount": 1,
                "charged_amount": 1,
                "profit_amount_total": 1,
                "items": 1,
                "created_at": 1,
                "updated_at": 1,
            },
        )
        if not doc:
            return jsonify({"success": False, "message": "Order not found"}), 404

        # datetime safe
        for k in ("created_at", "updated_at"):
            if isinstance(doc.get(k), datetime):
                doc[k] = doc[k].isoformat()

        return jsonify({"success": True, "order": doc}), 200
    except Exception:
        return jsonify({"success": False, "message": "Server error"}), 500
