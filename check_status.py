# check_status.py — public order status lookup by phone
from __future__ import annotations

from flask import Blueprint, render_template, request
from datetime import datetime
from typing import Any, Dict, List
import re

from db import db

orders_col = db["orders"]
stores_col = db["stores"]

check_status_bp = Blueprint("check_status", __name__)

_PHONE_RE = re.compile(r"^0\d{9}$")
_STORE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")

def _store_support_number(store_slug: str) -> str:
    if not store_slug or not _STORE_SLUG_RE.fullmatch(store_slug):
        return ""
    store = stores_col.find_one(
        {"slug": store_slug, "status": {"$ne": "deleted"}},
        {"contact.whatsapp_number": 1},
    ) or {}
    raw = str(((store.get("contact") or {}).get("whatsapp_number") or "")).strip()
    digits = re.sub(r"\D+", "", raw)
    if re.fullmatch(r"0\d{9}", digits):
        return "233" + digits[1:]
    # Keep existing international-format contacts working until agents update them.
    if re.fullmatch(r"233\d{9}", digits):
        return digits
    return ""

def _normalize_phone(raw: str) -> str:
    if not raw:
        return ""
    d = re.sub(r"\D+", "", str(raw))
    if d.startswith("233"):
        d = "0" + d[3:]
    if len(d) > 10:
        d = d[-10:]
    return d

def _fmt_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    try:
        return str(dt) if dt else ""
    except Exception:
        return ""

@check_status_bp.route("/check-status", methods=["GET"])
def check_status():
    """Render status page. If ?phone= is provided, show results."""
    phone = _normalize_phone(request.args.get("phone", ""))
    store_slug = (request.args.get("store") or "").strip().lower()
    if not _STORE_SLUG_RE.fullmatch(store_slug):
        store_slug = ""
    support_whatsapp = _store_support_number(store_slug) if store_slug else "233208165203"
    results: List[Dict[str, Any]] = []

    if phone and _PHONE_RE.match(phone):
        # Find orders that contain any item for this phone
        order_query: Dict[str, Any] = {"items.phone": phone}
        if store_slug:
            order_query["store_slug"] = store_slug
        cursor = orders_col.find(
            order_query,
            {
                "order_id": 1, "status": 1, "created_at": 1, "updated_at": 1,
                "items": 1, "paid_from": 1, "charged_amount": 1
            }
        ).sort([("created_at", -1)]).limit(50)

        for od in cursor:
            # Only include line-items for this phone in the rendered details
            items = [i for i in (od.get("items") or []) if str(i.get("phone")) == phone]
            results.append({
                "order_id": od.get("order_id"),
                "status": (od.get("status") or "").title(),
                "created_at": _fmt_dt(od.get("created_at")),
                "updated_at": _fmt_dt(od.get("updated_at")),
                "paid_amount": round(float(od.get("charged_amount") or 0.0), 2),
                # use 'lines' to avoid colliding with dict.items() in Jinja
                "lines": [{
                    "service": (it.get("serviceName") or "-"),
                    "value": (it.get("value") or "-"),
                    "amount": round(float(it.get("amount") or 0.0), 2),
                    "time": _fmt_dt(it.get("updated_at") or it.get("created_at") or od.get("updated_at") or od.get("created_at")),
                } for it in items]
            })

    return render_template(
        "check_status.html",
        phone=phone,
        results=results,
        store_slug=store_slug,
        support_whatsapp=support_whatsapp,
    )
