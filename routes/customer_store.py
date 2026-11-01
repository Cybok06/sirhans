from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Tuple, Optional
import re
import os

from bson import ObjectId
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from agent_code_utils import agent_codes_col
from announcement_utils import announcements_col
from db import db

customer_store_bp = Blueprint("customer_store", __name__)

# Canonical host enforcement for customer store dashboard
_CANONICAL_STORE_HOST = (os.getenv("STORE_PUBLIC_HOST", "www.hansmart.store") or "").strip().lower()
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

# Collections
stores_col               = db["stores"]
orders_col               = db["orders"]
users_col                = db["users"]
balances_col             = db["balances"]
transactions_col         = db["transactions"]
store_payouts_col        = db["store_payouts"]
store_payout_logs        = db["store_payout_logs"]
store_withdraw_requests  = db["store_withdraw_requests"]
store_accounts_col       = db["store_accounts"]
legacy_agent_code_col    = db["agent_code"]
STORE_USSD_CODE          = "*928*232#"


@customer_store_bp.route("/api/customer/store/<slug>/announcements", methods=["POST"])
def api_customer_store_create_announcement(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        owner_id = ObjectId(session["user_id"])
    except Exception:
        return jsonify({"success": False, "message": "Invalid session"}), 401

    store_doc = _ensure_owner_store(owner_id, slug)
    if not store_doc:
        return jsonify({"success": False, "message": "Store not found"}), 404

    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    message = str(data.get("message") or "").strip()
    end_date_raw = str(data.get("end_date") or "").strip()
    if not title:
        return jsonify({"success": False, "message": "Announcement title is required."}), 400
    if not message:
        return jsonify({"success": False, "message": "Announcement message is required."}), 400
    if len(title) > 120:
        return jsonify({"success": False, "message": "Title must be 120 characters or fewer."}), 400
    if len(message) > 2000:
        return jsonify({"success": False, "message": "Message must be 2,000 characters or fewer."}), 400
    if not end_date_raw:
        return jsonify({"success": False, "message": "Please select when the announcement should end."}), 400
    try:
        end_date = datetime.strptime(end_date_raw, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "message": "Please select a valid end date."}), 400
    if end_date < datetime.utcnow().date():
        return jsonify({"success": False, "message": "The end date cannot be in the past."}), 400

    owner = users_col.find_one({"_id": owner_id}, {"full_name": 1, "name": 1, "username": 1}) or {}
    creator_name = _owner_display_name(owner) or (store_doc.get("name") or "Store owner")
    now = datetime.utcnow()
    expires_at = datetime.combine(end_date, datetime.max.time())
    result = announcements_col.insert_one(
        {
            "title": title,
            "message": message,
            "tone": "info",
            "targets": ["store_page"],
            "store_slug": slug,
            "store_id": store_doc.get("_id"),
            "announcement_scope": "store_owner",
            "is_active": True,
            "expires_at": expires_at,
            "created_at": now,
            "updated_at": now,
            "created_by": owner_id,
            "created_by_name": creator_name,
        }
    )
    return jsonify(
        {
            "success": True,
            "announcement_id": str(result.inserted_id),
            "message": "Announcement published to your store successfully.",
        }
    ), 201


@customer_store_bp.route("/api/customer/store/<slug>/announcements", methods=["GET"])
def api_customer_store_announcement_history(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        owner_id = ObjectId(session["user_id"])
    except Exception:
        return jsonify({"success": False, "message": "Invalid session"}), 401

    store_doc = _ensure_owner_store(owner_id, slug)
    if not store_doc:
        return jsonify({"success": False, "message": "Store not found"}), 404

    docs = announcements_col.find(
        {
            "store_slug": slug,
            "announcement_scope": "store_owner",
            "created_by": owner_id,
            "deleted_at": {"$exists": False},
        },
        {"title": 1, "message": 1, "is_active": 1, "created_at": 1, "expires_at": 1},
    ).sort([("created_at", -1), ("_id", -1)])

    announcements = []
    now = datetime.utcnow()
    for doc in docs:
        created_at = doc.get("created_at")
        expires_at = doc.get("expires_at")
        is_expired = isinstance(expires_at, datetime) and expires_at < now
        announcements.append(
            {
                "id": str(doc.get("_id") or ""),
                "title": str(doc.get("title") or "").strip(),
                "message": str(doc.get("message") or "").strip(),
                "is_active": bool(doc.get("is_active", True)) and not is_expired,
                "is_expired": is_expired,
                "created_at": created_at.isoformat() + "Z" if isinstance(created_at, datetime) else "",
                "created_at_label": created_at.strftime("%b %d, %Y at %H:%M") if isinstance(created_at, datetime) else "",
                "expires_at": expires_at.isoformat() + "Z" if isinstance(expires_at, datetime) else "",
                "expires_at_label": expires_at.strftime("%b %d, %Y") if isinstance(expires_at, datetime) else "No end date",
            }
        )

    return jsonify({"success": True, "announcements": announcements})


@customer_store_bp.route("/api/customer/store/<slug>/announcements/<announcement_id>", methods=["DELETE"])
def api_customer_store_delete_announcement(slug: str, announcement_id: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        owner_id = ObjectId(session["user_id"])
    except Exception:
        return jsonify({"success": False, "message": "Invalid session"}), 401

    store_doc = _ensure_owner_store(owner_id, slug)
    if not store_doc:
        return jsonify({"success": False, "message": "Store not found"}), 404
    if not ObjectId.is_valid(announcement_id):
        return jsonify({"success": False, "message": "Announcement not found"}), 404

    result = announcements_col.update_one(
        {
            "_id": ObjectId(announcement_id),
            "store_slug": slug,
            "announcement_scope": "store_owner",
            "created_by": owner_id,
            "deleted_at": {"$exists": False},
        },
        {
            "$set": {
                "is_active": False,
                "deleted_at": datetime.utcnow(),
                "deleted_by": str(owner_id),
                "updated_at": datetime.utcnow(),
            }
        },
    )
    if result.modified_count != 1:
        return jsonify({"success": False, "message": "Announcement not found"}), 404

    return jsonify({"success": True, "message": "Announcement deleted successfully."})


# ---------- helpers ----------
def _day_range(d: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end

def _fmt_money(x: Any) -> float:
    try:
        return round(float(x or 0), 2)
    except Exception:
        return 0.0


def _order_base_total(order_doc: Dict[str, Any]) -> float:
    items = order_doc.get("items") or []
    base_total = 0.0
    found_base = False
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("base_amount") is not None:
                found_base = True
                base_total += _fmt_money(it.get("base_amount"))
    if found_base:
        return _fmt_money(base_total)
    return _fmt_money(order_doc.get("charged_amount", 0))

def _ensure_owner_store(user_id: ObjectId, slug: str) -> Optional[Dict[str, Any]]:
    return stores_col.find_one({"owner_id": user_id, "slug": slug, "status": {"$ne": "deleted"}})

def _latest_owner_store(user_id: ObjectId) -> Optional[Dict[str, Any]]:
    return stores_col.find_one(
        {"owner_id": user_id, "status": {"$ne": "deleted"}},
        sort=[("updated_at", -1), ("created_at", -1)]
    )

def _owner_agent_code_info(user_id: ObjectId) -> Dict[str, str]:
    query = {"user_id": user_id, "status": "active"}
    doc = agent_codes_col.find_one(query, {"agent_code": 1, "status": 1})
    if not doc:
        doc = legacy_agent_code_col.find_one(query, {"agent_code": 1, "status": 1})
    return {
        "code": str((doc or {}).get("agent_code") or ""),
        "status": str((doc or {}).get("status") or ""),
    }

def _owner_display_name(user_doc: Optional[Dict[str, Any]]) -> str:
    if not user_doc:
        return "Customer"
    for key in ("full_name", "name"):
        if user_doc.get(key):
            return str(user_doc[key]).strip()
    if user_doc.get("username"):
        return str(user_doc["username"]).strip()
    if user_doc.get("email"):
        return str(user_doc["email"]).split("@", 1)[0]
    return "Customer"

def _owner_wallet_balance(user_id: ObjectId) -> float:
    bal = balances_col.find_one({"user_id": user_id}) or {}
    return _fmt_money(bal.get("amount"))

def _profit_all_time(slug: str) -> float:
    order_profit_expr = {
        "$let": {
            "vars": {
                "items_profit": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                        }
                    }
                },
                "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
            },
            "in": {
                "$cond": [
                    {"$gt": ["$$items_profit", 0]},
                    "$$items_profit",
                    "$$legacy_profit",
                ]
            },
        }
    }
    pipeline = [
        {"$match": {"store_slug": slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {"_id": None, "p": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}}}},
    ]
    agg = list(orders_col.aggregate(pipeline))
    return _fmt_money(agg[0]["p"]) if agg else 0.0

def _withdrawn_so_far(slug: str) -> float:
    pipeline = [
        {"$match": {"type": "store_withdrawal", "status": "success", "meta.store_slug": slug}},
        {"$group": {"_id": None, "amt": {"$sum": {"$ifNull": ["$amount", 0]}}}},
    ]
    agg = list(transactions_col.aggregate(pipeline))
    return _fmt_money(agg[0]["amt"]) if agg else 0.0

def _withdrawable(slug: str) -> float:
    acct = store_accounts_col.find_one({"store_slug": slug}, {"total_profit_balance": 1}) or {}
    return _fmt_money(acct.get("total_profit_balance"))

def _iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    if isinstance(dt, str):
        return dt[:16]
    return ""

def _make_reference(prefix: str, oid: ObjectId) -> str:
    d = datetime.utcnow().strftime("%Y%m%d")
    tail = str(oid)[-6:].upper()
    return f"{prefix}-{d}-{tail}"

def _admin_guard() -> bool:
    return bool(session.get("user_id")) and (session.get("role") in ("admin", "superadmin"))

def _clean_phone(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()

def _extract_order_phones(order_doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Orders created by store_page.py store phone at items[].phone
    We derive:
      - phone_primary
      - phone_count
      - phone_summary
    """
    phones: List[str] = []

    top_phone = _clean_phone(order_doc.get("phone") or order_doc.get("customer_phone"))
    if top_phone:
        phones.append(top_phone)

    items = order_doc.get("items") or []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            p = _clean_phone(it.get("phone"))
            if p:
                phones.append(p)

    uniq: List[str] = []
    seen = set()
    for p in phones:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)

    phone_primary = uniq[0] if uniq else ""
    phone_count = len(uniq)

    if not uniq:
        summary = ""
    else:
        show = uniq[:4]
        summary = ", ".join(show)
        if len(uniq) > 4:
            summary += f" (+{len(uniq)-4} more)"

    return {
        "phone_primary": phone_primary or None,
        "phone_count": phone_count,
        "phone_summary": summary or None,
    }

def _gather_dashboard(slug: str) -> Dict[str, Any]:
    today = datetime.utcnow().date()
    d0, d1 = _day_range(today)

    order_profit_expr = {
        "$let": {
            "vars": {
                "items_profit": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                        }
                    }
                },
                "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
            },
            "in": {
                "$cond": [
                    {"$gt": ["$$items_profit", 0]},
                    "$$items_profit",
                    "$$legacy_profit",
                ]
            },
        }
    }

    pipeline_totals = [
        {"$match": {"store_slug": slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {
            "_id": None,
            "total_sales": {"$sum": {"$ifNull": ["$total_amount", 0]}},
            "total_profit": {"$sum": {"$ifNull": ["$store_profit_sum", 0]}},
            "orders_count": {"$sum": 1},
        }},
    ]
    agg_tot = list(orders_col.aggregate(pipeline_totals))
    all_time_sales  = _fmt_money(agg_tot[0].get("total_sales") if agg_tot else 0)
    all_time_profit = _fmt_money(agg_tot[0].get("total_profit") if agg_tot else 0)
    orders_count    = int(agg_tot[0].get("orders_count") if agg_tot else 0)

    pipeline_today_profit = [
        {"$match": {"store_slug": slug, "created_at": {"$gte": d0, "$lt": d1}}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {"_id": None, "profit_today": {"$sum": {"$ifNull": ["$store_profit_sum", 0]}}}},
    ]
    agg_today = list(orders_col.aggregate(pipeline_today_profit))
    profit_today = _fmt_money(agg_today[0].get("profit_today") if agg_today else 0)

    pipeline_top_offers = [
        {"$match": {"store_slug": slug}},
        {"$unwind": {"path": "$items", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id": {
                "service": {"$ifNull": ["$items.serviceName", "Unknown Service"]},
                "label":   {"$ifNull": ["$items.value", "-"]},
            },
            "count":   {"$sum": 1},
            "revenue": {"$sum": {"$ifNull": ["$items.amount", 0]}},
        }},
        {"$sort": {"count": -1, "revenue": -1}},
        {"$limit": 8},
    ]
    top_offers_raw = list(orders_col.aggregate(pipeline_top_offers))
    top_offers = [{
        "service": x["_id"]["service"],
        "label":   x["_id"]["label"],
        "count":   int(x.get("count", 0)),
        "revenue": _fmt_money(x.get("revenue", 0)),
    } for x in top_offers_raw]

    recent_orders_cur = (
        orders_col.find({"store_slug": slug})
        .sort("created_at", -1)
        .limit(10)
    )

    recent_orders: List[Dict[str, Any]] = []
    for o in recent_orders_cur:
        items = o.get("items") or []
        store_profit_total = 0.0
        found_store_profit = False
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                sp = it.get("store_profit_amount")
                if sp is not None:
                    found_store_profit = True
                    store_profit_total += _fmt_money(sp)
        if not found_store_profit:
            store_profit_total = _fmt_money(o.get("profit_amount_total", 0))

        phone_info = _extract_order_phones(o)
        recent_orders.append({
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "base_amount_total": _order_base_total(o),
            "total_amount":        _fmt_money(o.get("total_amount", 0)),
            "profit_amount_total": store_profit_total,
            "charged_amount":      _fmt_money(o.get("charged_amount", 0)),
            "items_count": len(o.get("items") or []),
            "created_at": o.get("created_at"),
            **phone_info,
        })

    return {
        "today": today,
        "all_time_sales": all_time_sales,
        "profit_today": profit_today,
        "all_time_profit": all_time_profit,
        "orders_count": orders_count,
        "top_offers": top_offers,
        "recent_orders": recent_orders,
        "withdrawable": _withdrawable(slug),
    }


# ---------- Pages ----------
@customer_store_bp.route("/customer/store", methods=["GET"])
def customer_store_home():
    redir = _store_host_redirect()
    if redir:
        return redir
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))

    owner_id = ObjectId(session["user_id"])
    owner = users_col.find_one({"_id": owner_id}, {"full_name": 1, "name": 1, "username": 1, "email": 1})
    store_doc = _latest_owner_store(owner_id)
    agent_code_info = _owner_agent_code_info(owner_id)

    if not store_doc:
        today = datetime.utcnow().date()
        return render_template(
            "customer_store.html",
            store=None,
            owner_name=_owner_display_name(owner),
            all_time_sales=0.00,
            profit_today=0.00,
            all_time_profit=0.00,
            orders_count=0,
            top_offers=[],
            recent_orders=[],
            withdrawable=0.00,
            wallet_balance=_owner_wallet_balance(owner_id),
            today_str=today.strftime("%b %d, %Y"),
            slug=None,
            store_host=_CANONICAL_STORE_HOST,
            store_ussd_code=STORE_USSD_CODE,
            agent_code=agent_code_info["code"],
            agent_code_status=agent_code_info["status"],
        )

    slug = store_doc.get("slug")
    k = _gather_dashboard(slug)

    return render_template(
        "customer_store.html",
        store=store_doc,
        owner_name=_owner_display_name(owner),
        all_time_sales=k["all_time_sales"],
        profit_today=k["profit_today"],
        all_time_profit=k["all_time_profit"],
        orders_count=k["orders_count"],
        top_offers=k["top_offers"],
        recent_orders=k["recent_orders"],
        withdrawable=k["withdrawable"],
        wallet_balance=_owner_wallet_balance(owner_id),
        today_str=k["today"].strftime("%b %d, %Y"),
        slug=slug,
        store_host=_CANONICAL_STORE_HOST,
        store_ussd_code=STORE_USSD_CODE,
        agent_code=agent_code_info["code"],
        agent_code_status=agent_code_info["status"],
    )

@customer_store_bp.route("/customer/store/<slug>", methods=["GET"])
def customer_store_dashboard(slug: str):
    redir = _store_host_redirect()
    if redir:
        return redir
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))

    owner_id = ObjectId(session["user_id"])
    store_doc = _ensure_owner_store(owner_id, slug)
    if not store_doc:
        return redirect(url_for("customer_store.customer_store_home"))

    k = _gather_dashboard(slug)
    owner = users_col.find_one({"_id": owner_id}, {"full_name": 1, "name": 1, "username": 1, "email": 1})
    agent_code_info = _owner_agent_code_info(owner_id)

    return render_template(
        "customer_store.html",
        store=store_doc,
        owner_name=_owner_display_name(owner),
        all_time_sales=k["all_time_sales"],
        profit_today=k["profit_today"],
        all_time_profit=k["all_time_profit"],
        orders_count=k["orders_count"],
        top_offers=k["top_offers"],
        recent_orders=k["recent_orders"],
        withdrawable=k["withdrawable"],
        wallet_balance=_owner_wallet_balance(owner_id),
        today_str=k["today"].strftime("%b %d, %Y"),
        slug=slug,
        store_host=_CANONICAL_STORE_HOST,
        store_ussd_code=STORE_USSD_CODE,
        agent_code=agent_code_info["code"],
        agent_code_status=agent_code_info["status"],
    )


# ---------- Customer APIs ----------
@customer_store_bp.route("/api/customer/store/<slug>/payout_snapshot", methods=["GET"])
def api_customer_store_payout_snapshot(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401
    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    payout = store_payouts_col.find_one(
        {"owner_id": owner_id, "store_slug": slug},
        {"recipient_name": 1, "msisdn": 1, "network": 1}
    ) or {}
    return jsonify({"success": True, "payout": {
        "recipient_name": payout.get("recipient_name"),
        "msisdn": payout.get("msisdn"),
        "network": payout.get("network"),
    }})

@customer_store_bp.route("/api/customer/store/<slug>/withdrawals", methods=["GET"])
def api_customer_store_withdrawals(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401
    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except Exception:
        limit = 20
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    skip = (page - 1) * limit

    cur = transactions_col.find(
        {"type": "store_withdrawal", "status": "success", "meta.store_slug": slug}
    ).sort("created_at", -1).skip(skip).limit(limit)

    items = []
    for t in cur:
        meta = (t.get("meta") or {})
        note = meta.get("note") or ""
        method = meta.get("method")
        if method == "momo":
            note = note or "Paid to MoMo"
        elif method == "wallet":
            note = note or "Credited to wallet"

        items.append({
            "reference": t.get("reference"),
            "amount": _fmt_money(t.get("amount")),
            "created_at": _iso(t.get("created_at")),
            "verified_at": _iso(t.get("verified_at")),
            "note": note or "Paid",
        })

    total = transactions_col.count_documents({"type": "store_withdrawal", "status": "success", "meta.store_slug": slug})
    return jsonify({"success": True, "items": items, "page": page, "limit": limit, "total": total})

@customer_store_bp.route("/api/customer/store/<slug>/withdraw/requests", methods=["GET"])
def api_customer_store_withdraw_requests(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except Exception:
        limit = 10

    cur = store_withdraw_requests.find(
        {"owner_id": owner_id, "store_slug": slug},
        sort=[("created_at", -1)]
    ).limit(limit)

    items = []
    for r in cur:
        items.append({
            "id": str(r.get("_id")),
            "reference": r.get("reference"),
            "amount": _fmt_money(r.get("amount")),
            "method": r.get("method"),
            "status": r.get("status"),
            "created_at": _iso(r.get("created_at")),
            "updated_at": _iso(r.get("updated_at")),
        })

    return jsonify({"success": True, "items": items})

@customer_store_bp.route("/api/customer/store/<slug>/withdraw/request", methods=["POST"])
def api_customer_store_request_withdraw(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    payload = request.get_json(silent=True) or {}
    amount = _fmt_money(payload.get("amount"))
    method = str(payload.get("method") or "momo").strip().lower()

    if method not in ("momo", "wallet"):
        return jsonify({"success": False, "message": "Invalid method"}), 400
    if amount <= 0:
        return jsonify({"success": False, "message": "Enter a valid amount"}), 400

    max_allowed = _withdrawable(slug)
    if amount - max_allowed > 1e-9:
        return jsonify({"success": False, "message": f"Amount exceeds profit balance (GHS {max_allowed:.2f})"}), 400

    payout_snapshot = None
    if method == "momo":
        payout = store_payouts_col.find_one(
            {"owner_id": owner_id, "store_slug": slug},
            {"recipient_name": 1, "msisdn": 1, "network": 1}
        ) or {}
        payout_snapshot = {
            "recipient_name": payout.get("recipient_name"),
            "msisdn": payout.get("msisdn"),
            "network": payout.get("network"),
        }

    recent_pending = store_withdraw_requests.find_one({
        "owner_id": owner_id,
        "store_slug": slug,
        "status": "pending",
        "amount": amount,
        "created_at": {"$gte": datetime.utcnow() - timedelta(minutes=2)}
    })
    if recent_pending:
        return jsonify({"success": True, "message": "Request already submitted", "id": str(recent_pending["_id"])})

    doc_id = ObjectId()
    doc = {
        "_id": doc_id,
        "reference": _make_reference("WDR", doc_id),
        "owner_id": owner_id,
        "store_slug": slug,
        "amount": amount,
        "method": method,
        "payout_snapshot": payout_snapshot,
        "status": "pending",
        "note": "",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    store_withdraw_requests.insert_one(doc)

    return jsonify({"success": True, "id": str(doc_id), "reference": doc["reference"]})


@customer_store_bp.route("/api/customer/store/<slug>/orders/search", methods=["GET"])
def api_customer_store_orders_search(slug: str):
    """
    Default returns latest 10.
    If q provided, searches:
      - order_id (regex)
      - items.phone (regex)
      - phone / customer_phone (regex)

    Critical Fix:
      - No projection collision (do NOT project 'items' and 'items.phone' together)
      - Use aggregation to return a LIGHT items array with only {phone}
    """
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    q_raw = (request.args.get("q") or "").strip()

    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except Exception:
        limit = 10

    q_digits = re.sub(r"\D+", "", q_raw or "").strip()

    match: Dict[str, Any] = {"store_slug": slug}

    if q_raw:
        rx_any = {"$regex": re.escape(q_raw), "$options": "i"}

        or_terms: List[Dict[str, Any]] = [
            {"order_id": rx_any},
            {"items.phone": rx_any},
            {"phone": rx_any},
            {"customer_phone": rx_any},
        ]

        if q_digits and len(q_digits) >= 6:
            rx_d = {"$regex": re.escape(q_digits), "$options": "i"}
            or_terms.extend([
                {"items.phone": rx_d},
                {"phone": rx_d},
                {"customer_phone": rx_d},
            ])

        match["$or"] = or_terms

    try:
        order_profit_expr = {
            "$let": {
                "vars": {
                    "items_profit": {
                        "$sum": {
                            "$map": {
                                "input": {"$ifNull": ["$items", []]},
                                "as": "it",
                                "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                            }
                        }
                    },
                    "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
                },
                "in": {
                    "$cond": [
                        {"$gt": ["$$items_profit", 0]},
                        "$$items_profit",
                        "$$legacy_profit",
                    ]
                },
            }
        }
        pipeline = [
            {"$match": match},
            {"$sort": {"created_at": -1}},
            {"$limit": int(limit)},
            {"$addFields": {"store_profit_sum": order_profit_expr}},
            {
                "$project": {
                    "_id": 0,
                    "order_id": 1,
                    "status": 1,
                    "total_amount": 1,
                    "profit_amount_total": "$store_profit_sum",
                    "charged_amount": 1,
                    "created_at": 1,
                    "phone": 1,
                    "customer_phone": 1,

                    "items_count": {"$size": {"$ifNull": ["$items", []]}},

                    # Light items for phone extraction
                    "items": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"phone": {"$ifNull": ["$$it.phone", None]}}
                        }
                    },
                }
            },
        ]
        docs = list(orders_col.aggregate(pipeline))
    except Exception as e:
        return jsonify({"success": False, "message": f"Search failed: {str(e)}"}), 500

    items: List[Dict[str, Any]] = []
    for o in docs:
        phone_info = _extract_order_phones(o)
        items.append({
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "base_amount_total": _order_base_total(o),
            "total_amount": _fmt_money(o.get("total_amount", 0)),
            "profit_amount_total": _fmt_money(o.get("profit_amount_total", 0)),
            "charged_amount": _fmt_money(o.get("charged_amount", 0)),
            "items_count": int(o.get("items_count") or 0),
            "created_at": _iso(o.get("created_at")),
            **phone_info,
        })

    return jsonify({"success": True, "items": items})


# ---------- Admin APIs ----------
@customer_store_bp.route("/api/admin/store/withdraw/requests", methods=["GET"])
def api_admin_store_withdraw_requests():
    if not _admin_guard():
        return jsonify({"success": False, "message": "Admin login required"}), 401

    status = (request.args.get("status") or "").strip().lower()
    slug = (request.args.get("slug") or "").strip()
    owner_id_raw = (request.args.get("owner_id") or "").strip()
    q = (request.args.get("q") or "").strip()

    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50

    query: Dict[str, Any] = {}
    if status in ("pending", "paid", "rejected"):
        query["status"] = status
    if slug:
        query["store_slug"] = slug
    if owner_id_raw:
        try:
            query["owner_id"] = ObjectId(owner_id_raw)
        except Exception:
            return jsonify({"success": False, "message": "Invalid owner_id"}), 400
    if q:
        query["reference"] = {"$regex": re.escape(q), "$options": "i"}

    cur = store_withdraw_requests.find(query).sort("created_at", -1).limit(limit)

    items = []
    for r in cur:
        payout = r.get("payout_snapshot") or {}
        items.append({
            "id": str(r.get("_id")),
            "reference": r.get("reference"),
            "store_slug": r.get("store_slug"),
            "owner_id": str(r.get("owner_id")),
            "amount": _fmt_money(r.get("amount")),
            "method": r.get("method"),
            "status": r.get("status"),
            "created_at": _iso(r.get("created_at")),
            "updated_at": _iso(r.get("updated_at")),
            "payout_snapshot": payout,
        })

    return jsonify({"success": True, "items": items})

@customer_store_bp.route("/api/admin/store/withdraw/<request_id>/status", methods=["POST"])
def api_admin_store_withdraw_update_status(request_id: str):
    if not _admin_guard():
        return jsonify({"success": False, "message": "Admin login required"}), 401

    try:
        rid = ObjectId(request_id)
    except Exception:
        return jsonify({"success": False, "message": "Invalid request id"}), 400

    payload = request.get_json(silent=True) or {}
    new_status = str(payload.get("status") or "").strip().lower()
    note = str(payload.get("note") or "").strip()

    if new_status not in ("pending", "paid", "rejected"):
        return jsonify({"success": False, "message": "Invalid status"}), 400

    req_doc = store_withdraw_requests.find_one({"_id": rid})
    if not req_doc:
        return jsonify({"success": False, "message": "Request not found"}), 404

    old_status = str(req_doc.get("status") or "pending").lower()
    if old_status == new_status and (note == (req_doc.get("note") or "")):
        return jsonify({"success": True, "message": "No changes"})

    now = datetime.utcnow()
    admin_id = ObjectId(session["user_id"])

    if new_status == "paid":
        if old_status == "paid":
            store_withdraw_requests.update_one(
                {"_id": rid},
                {"$set": {"note": note, "updated_at": now}}
            )
            return jsonify({"success": True, "message": "Already paid"})

        store_withdraw_requests.update_one(
            {"_id": rid},
            {"$set": {
                "status": "paid",
                "note": note,
                "paid_at": now,
                "paid_by": admin_id,
                "updated_at": now,
            }}
        )

        existing_tx = transactions_col.find_one({
            "type": "store_withdrawal",
            "meta.request_id": str(rid),
            "status": "success"
        })
        if existing_tx:
            return jsonify({"success": True, "message": "Paid (transaction already exists)"})

        amount = _fmt_money(req_doc.get("amount"))
        method = str(req_doc.get("method") or "").lower()
        store_slug = req_doc.get("store_slug")
        owner_id = req_doc.get("owner_id")

        if method == "wallet":
            balances_col.update_one(
                {"user_id": owner_id},
                {"$inc": {"amount": amount}, "$setOnInsert": {"created_at": now}, "$set": {"updated_at": now}},
                upsert=True
            )

        transactions_col.insert_one({
            "type": "store_withdrawal",
            "status": "success",
            "amount": amount,
            "reference": req_doc.get("reference") or _make_reference("WD", rid),
            "created_at": now,
            "verified_at": now,
            "meta": {
                "store_slug": store_slug,
                "owner_id": str(owner_id),
                "request_id": str(rid),
                "method": method,
                "note": note or ("Credited to wallet" if method == "wallet" else "Paid to MoMo"),
                "payout_snapshot": req_doc.get("payout_snapshot") if method == "momo" else None,
            }
        })

        return jsonify({"success": True, "message": "Marked paid"})

    store_withdraw_requests.update_one(
        {"_id": rid},
        {"$set": {"status": new_status, "note": note, "updated_at": now, "updated_by": admin_id}}
    )
    return jsonify({"success": True, "message": f"Marked {new_status}"})


# ---------- Payout settings ----------
@customer_store_bp.route("/customer/store/<slug>/payout", methods=["GET"])
def customer_store_payout_page(slug: str):
    redir = _store_host_redirect()
    if redir:
        return redir
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))
    owner_id = ObjectId(session["user_id"])
    store = _ensure_owner_store(owner_id, slug)
    if not store:
        return redirect(url_for("customer_store.customer_store_home"))

    payout = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    hist = list(store_payout_logs.find({"owner_id": owner_id, "store_slug": slug}).sort("created_at", -1).limit(100))

    return render_template(
        "customer_store_payout.html",
        store=store,
        current=payout,
        history=hist,
        withdrawable=_withdrawable(slug),
        wallet_balance=_owner_wallet_balance(owner_id),
        store_host=_CANONICAL_STORE_HOST,
    )

@customer_store_bp.route("/customer/store/<slug>/payout", methods=["POST"])
def customer_store_payout_save(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))
    owner_id = ObjectId(session["user_id"])
    store = _ensure_owner_store(owner_id, slug)
    if not store:
        return redirect(url_for("customer_store.customer_store_home"))

    name = (request.form.get("recipient_name") or "").strip()
    phone = (request.form.get("msisdn") or "").strip()
    network = (request.form.get("network") or "").strip().upper()

    valid_nets = {"MTN", "VODAFONE", "AIRTELTIGO"}
    if network not in valid_nets:
        return render_template(
            "customer_store_payout.html",
            store=store,
            current={"recipient_name": name, "msisdn": phone, "network": network},
            history=list(store_payout_logs.find({"owner_id": owner_id, "store_slug": slug}).sort("created_at", -1)),
            error="Select a valid network.",
            withdrawable=_withdrawable(slug),
            wallet_balance=_owner_wallet_balance(owner_id),
            store_host=_CANONICAL_STORE_HOST,
        )

    def _normalize_phone(raw: str) -> str:
        p = raw.replace(" ", "").replace("-", "").replace("+", "")
        if p.startswith("0") and len(p) == 10:
            p = "233" + p[1:]
        if p.startswith("233") and len(p) == 12:
            return p
        return raw.strip()

    phone_norm = _normalize_phone(phone)

    prev = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    doc = {
        "owner_id": owner_id,
        "store_slug": slug,
        "recipient_name": name,
        "msisdn": phone_norm,
        "network": network,
        "updated_at": datetime.utcnow(),
        "created_at": prev.get("created_at") or datetime.utcnow(),
    }
    store_payouts_col.update_one(
        {"owner_id": owner_id, "store_slug": slug},
        {"$set": doc},
        upsert=True
    )

    changes: Dict[str, Dict[str, Any]] = {}
    for k in ("recipient_name", "msisdn", "network"):
        old_v = prev.get(k)
        new_v = doc.get(k)
        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}
    if changes:
        store_payout_logs.insert_one({
            "owner_id": owner_id,
            "store_slug": slug,
            "changes": changes,
            "created_at": datetime.utcnow(),
        })

    return redirect(url_for("customer_store.customer_store_payout_page", slug=slug))
