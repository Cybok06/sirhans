from flask import Blueprint, render_template, session, redirect, url_for, request, flash, jsonify, Request
from db import db
from datetime import datetime
from bson import ObjectId
from werkzeug.utils import secure_filename
import os
import json
import uuid
import re
from ast import literal_eval
from collections import defaultdict

admin_services_bp = Blueprint("admin_services", __name__)
services_col = db["services"]
users_col = db["users"]                     # customers live here
service_profits_col = db["service_profits"] # legacy collection; pricing is now manual per offer

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")

def _ensure_upload_folder():
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def _require_admin():
    return session.get("role") == "admin"

_ALLOWED_TYPES = {"API", "OFF"}
def _norm_type(t: str | None) -> str | None:
    if not t:
        return None
    t = t.strip().upper()
    return t if t in _ALLOWED_TYPES else None

_ALLOWED_PROVIDERS = {"portal02", "dataconnect", "codecraft", "datakazina", "skplug"}
def _norm_provider(p: str | None) -> str | None:
    if not p:
        return None
    p = str(p).strip().lower()
    return p if p in _ALLOWED_PROVIDERS else None

def _to_float(s):
    try:
        return float(s)
    except Exception:
        return None

def _to_int(s):
    try:
        if isinstance(s, str):
            s = s.replace(",", "").strip()
        return int(float(s))
    except Exception:
        return None

_MB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*MB\s*$", re.I)
_GB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*G(?:B|IG)?\s*$", re.I)
_INT_RE = re.compile(r"^\s*[\d,]+\s*$")

def _parse_volume_to_mb(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v)))
    txt = str(v).strip()

    m = _MB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val))

    m = _GB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val * 1000))

    if _INT_RE.match(txt):
        return int(txt.replace(",", ""))

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "volume" in as_json:
                return _to_int(as_json["volume"])
    except Exception:
        pass

    try:
        d = literal_eval(txt)
        if isinstance(d, dict) and "volume" in d:
            return _to_int(d["volume"])
    except Exception:
        pass

    return None

def _format_volume(vol_mb):
    if vol_mb is None:
        return "-"
    try:
        vol_mb = float(vol_mb)
    except Exception:
        return "-"
    if vol_mb >= 1000:
        gb = vol_mb / 1000.0
        return f"{int(gb)}GB" if abs(gb - round(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(vol_mb)}MB"

def _extract_pkg_id(value_raw):
    if value_raw is None:
        return None
    if isinstance(value_raw, (int, float)):
        return _to_int(value_raw)

    txt = str(value_raw).strip()
    if _INT_RE.match(txt):
        return _to_int(txt)

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "id" in as_json:
                return _to_int(as_json["id"])
    except Exception:
        pass

    try:
        d = literal_eval(txt)
        if isinstance(d, dict) and "id" in d:
            return _to_int(d["id"])
    except Exception:
        pass

    return None

def _to_mtn_value_string(pkg_id: int | None, volume_mb: int | None, fallback_value_raw: str | None):
    if volume_mb is None:
        volume_mb = _parse_volume_to_mb(fallback_value_raw)
    volume_mb = _to_int(volume_mb) if volume_mb is not None else None
    pkg_id = _to_int(pkg_id) if pkg_id is not None else None
    if pkg_id is None or volume_mb is None:
        return None
    return f"{{'id': {pkg_id}, 'volume': {volume_mb}}}"

def _generate_random_offer_id(used_ids: set[int], used_volumes: set[int]) -> int:
    for _ in range(256):
        candidate = (uuid.uuid4().int % 900000) + 100000
        if candidate not in used_ids and candidate not in used_volumes:
            used_ids.add(candidate)
            return candidate
    candidate = 1000000
    while candidate in used_ids or candidate in used_volumes:
        candidate += 1
    used_ids.add(candidate)
    return candidate

def _compute_value_text_from_mtn_string(value_str: str):
    if not isinstance(value_str, str):
        return "-"
    try:
        d = literal_eval(value_str)
        if not isinstance(d, dict):
            return value_str
        vol_mb = _to_int(d.get("volume"))
        return _format_volume(vol_mb)
    except Exception:
        vol_mb = _parse_volume_to_mb(value_str)
        if vol_mb is not None:
            return _format_volume(vol_mb)
        return value_str or "-"

def _with_customer_price(base_offers: list[dict], store_offers: list[dict] | None = None) -> list[dict]:
    merged: list[dict] = []
    store_rows = store_offers or []
    for idx, base in enumerate(base_offers or []):
        row = dict(base or {})
        store_row = store_rows[idx] if idx < len(store_rows) else {}
        customer_price = store_row.get("amount")
        if customer_price is None:
            customer_price = row.get("amount")
        row["customer_price"] = customer_price
        merged.append(row)
    return merged

# ===========================
# OFFERS PARSER (WITH PREFIX)
# ===========================
def _parse_offers(req: Request, prefix: str = "offers"):
    """
    prefix='offers'               -> uses offers_amount[], offers_value[]
    prefix='elite_offers'         -> uses elite_offers_amount[], elite_offers_value[]
    prefix='professional_offers'  -> uses professional_offers_amount[], professional_offers_value[]
    prefix='store_offers'         -> uses store_offers_amount[], store_offers_value[]
    prefix='public_offers'        -> uses public_offers_amount[], public_offers_value[]
    """
    amount_key = f"{prefix}_amount[]"
    value_key  = f"{prefix}_value[]"
    volume_key = f"{prefix}_volume[]"
    pkg_id_key = f"{prefix}_pkg_id[]"

    amounts = req.form.getlist(amount_key)
    values_freetext = req.form.getlist(value_key)
    volumes = req.form.getlist(volume_key)
    pkg_ids = req.form.getlist(pkg_id_key)

    n = max(len(amounts), len(values_freetext), len(volumes), len(pkg_ids))
    offers = []
    used_ids: set[int] = set()
    used_volumes: set[int] = set()

    for i in range(n):
        amount = (amounts[i] if i < len(amounts) else "").strip()
        value_txt = (values_freetext[i] if i < len(values_freetext) else "").strip()
        volume_txt = (volumes[i] if i < len(volumes) else "").strip()
        pkg_id_txt = (pkg_ids[i] if i < len(pkg_ids) else "").strip()

        if not amount and not value_txt and not volume_txt:
            continue

        base_amount = _to_float(amount)
        value_source = volume_txt or value_txt

        pkg_id = _to_int(pkg_id_txt) if pkg_id_txt else _extract_pkg_id(value_txt)
        vol_mb = _parse_volume_to_mb(value_source)

        if vol_mb is None:
            continue

        if pkg_id is None or pkg_id in used_ids or pkg_id == vol_mb or pkg_id in used_volumes:
            pkg_id = _generate_random_offer_id(used_ids, used_volumes)
        else:
            used_ids.add(pkg_id)
        used_volumes.add(int(vol_mb))

        value_str = _to_mtn_value_string(pkg_id, vol_mb, value_source)
        if value_str is None:
            value_str = f"{{'id': {int(pkg_id)}, 'volume': {int(vol_mb)}}}"

        offers.append({
            "amount": base_amount,
            "value": value_str,
            "profit": None
        })

    return offers

def _overlay_offer_prices(base_offers: list[dict], price_offers: list[dict]) -> list[dict]:
    """
    Keep values locked to the base service offer rows and overlay only prices.
    Missing custom prices fall back to the base Normal price for that row.
    """
    merged: list[dict] = []
    for idx, base in enumerate(base_offers or []):
        src = price_offers[idx] if idx < len(price_offers or []) else {}
        amount = src.get("amount")
        if amount is None:
            amount = base.get("amount")
        merged.append({
            "amount": amount,
            "value": base.get("value"),
            "profit": None
        })
    return merged

def _display_name(user_doc):
    nm = (user_doc.get("business_name") or "").strip()
    if nm:
        return nm
    fn = (user_doc.get("first_name") or "").strip()
    ln = (user_doc.get("last_name") or "").strip()
    full = (" ".join([fn, ln])).strip()
    return full or (user_doc.get("username") or user_doc.get("phone") or str(user_doc.get("_id")))

# =======================
#      PAGE ROUTES
# =======================
@admin_services_bp.route("/admin/services", methods=["GET"])
def manage_services():
    if not _require_admin():
        return redirect(url_for("login.login"))

    services = list(services_col.find({}, {
        "name": 1,
        "image_url": 1,
        "offers": 1,
        "elite_offers": 1,
        "professional_offers": 1,
        "store_offers": 1,   # NEW
        "public_offers": 1,
        "pricing_model": 1,
        "created_at": 1,
        "type": 1,
        "provider": 1,
        "status": 1,
        "availability": 1,
        "display": 1,
        "store_display": 1,
        "public_display": 1,
    }).sort([("_id", -1)]))

    for s in services:
        s["_id_str"] = str(s["_id"])
        s["pricing_model"] = s.get("pricing_model") or "manual"
        s["display"] = (s.get("display") or "ON").upper()
        s["store_display"] = (s.get("store_display") or "ON").upper()
        s["public_display"] = (s.get("public_display") or "ON").upper()

        # compute value_text for all manual price ladders
        for key in ("offers", "elite_offers", "professional_offers", "store_offers", "public_offers"):
            if isinstance(s.get(key), list):
                for of in s[key]:
                    v = of.get("value")
                    of["value_text"] = _compute_value_text_from_mtn_string(v) if isinstance(v, str) else "-"
                    of["pkg_id"] = _extract_pkg_id(v)
                    of["volume_mb"] = _parse_volume_to_mb(v)

    users_cursor = users_col.find(
        {"role": "customer"},
        {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "business_name": 1}
    ).sort([("first_name", 1), ("last_name", 1)])

    customers = [{"_id": str(u["_id"]), "name": _display_name(u)} for u in users_cursor]

    return render_template("admin_services.html", services=services, customers=customers)

@admin_services_bp.route("/admin/services/create", methods=["POST"])
def create_service():
    if not _require_admin():
        return redirect(url_for("login.login"))

    service_name = (request.form.get("service_name") or "").strip()
    image_url = (request.form.get("image_url") or "").strip()
    service_type = _norm_type(request.form.get("service_type")) or "API"
    provider = _norm_provider(request.form.get("provider")) or "portal02"

    if not service_name:
        flash("Service name is required.", "danger")
        return redirect(url_for("admin_services.manage_services"))
    if not image_url:
        flash("Please upload/select an image for the service.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    offers = _parse_offers(request, "offers")
    elite_offers = _parse_offers(request, "elite_offers") or list(offers)
    professional_offers = _parse_offers(request, "professional_offers") or list(offers)

    # Optionally copy normal prices to store/public on create.
    copy_default_to_store = (request.form.get("copy_default_to_store") or "").strip()
    copy_default_to_public = (request.form.get("copy_default_to_public") or "").strip()
    store_offers = [dict(x) for x in offers] if copy_default_to_store else []
    public_offers = [dict(x) for x in offers] if copy_default_to_public else []
    offers = _with_customer_price(offers, store_offers)

    doc = {
        "name": service_name,
        "image_url": image_url,
        "offers": offers,
        "elite_offers": elite_offers,
        "professional_offers": professional_offers,
        "store_offers": store_offers,
        "public_offers": public_offers,
        "pricing_model": "manual",
        "default_profit_percent": 0.0,
        "type": service_type,
        "provider": provider,
        "status": "OPEN",
        "availability": "AVAILABLE",
        "display": "ON",
        "store_display": "ON",
        "public_display": "ON",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    services_col.insert_one(doc)
    flash("Service added successfully.", "success")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/admin/services/<service_id>/update", methods=["POST"])
def update_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        _id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    service = services_col.find_one({"_id": _id})
    if not service:
        flash("Service not found.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    # Manual pricing sets.
    offers = _parse_offers(request, "offers")
    elite_offers = _overlay_offer_prices(offers, _parse_offers(request, "elite_offers"))
    professional_offers = _overlay_offer_prices(offers, _parse_offers(request, "professional_offers"))
    store_offers = _overlay_offer_prices(offers, _parse_offers(request, "store_offers"))
    public_offers = _overlay_offer_prices(offers, _parse_offers(request, "public_offers"))
    offers = _with_customer_price(offers, store_offers)

    update_doc = {
        "offers": offers,
        "elite_offers": elite_offers,
        "professional_offers": professional_offers,
        "store_offers": store_offers,
        "public_offers": public_offers,
        "pricing_model": "manual",
        "default_profit_percent": 0.0,
        "updated_at": datetime.utcnow()
    }
    services_col.update_one({"_id": _id}, {"$set": update_doc})
    flash("Service prices updated successfully.", "success")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/admin/services/<service_id>/delete", methods=["POST"])
def delete_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        _id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_services.manage_services"))

    svc = services_col.find_one({"_id": _id})
    res = services_col.delete_one({"_id": _id})

    if res.deleted_count:
        try:
            if svc and isinstance(svc.get("image_url"), str) and svc["image_url"].startswith("/uploads/"):
                _ensure_upload_folder()
                fname = svc["image_url"].replace("/uploads/", "")
                fpath = os.path.join(UPLOAD_FOLDER, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
        except Exception:
            pass
        service_profits_col.delete_many({"service_id": _id})
        flash("Service deleted.", "info")
    else:
        flash("Service not found or already deleted.", "warning")

    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/upload_service_image", methods=["POST"])
def upload_service_image():
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    if "image" not in request.files:
        return jsonify({"success": False, "error": "No file part 'image'"}), 400

    file = request.files["image"]
    if not file or file.filename.strip() == "":
        return jsonify({"success": False, "error": "No selected file"}), 400

    if not _allowed_file(file.filename):
        return jsonify({"success": False, "error": "Invalid file type"}), 400

    _ensure_upload_folder()

    base, ext = os.path.splitext(secure_filename(file.filename))
    filename = f"{base}_{uuid.uuid4().hex[:8]}{ext.lower()}"
    target_path = os.path.join(UPLOAD_FOLDER, filename)

    file.save(target_path)
    file_url = f"/uploads/{filename}"
    return jsonify({"success": True, "url": file_url}), 200

# =======================
#   PROFIT ENDPOINTS
# =======================
@admin_services_bp.route("/admin/services/<service_id>/profit/default", methods=["POST"])
def set_service_default_profit(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    flash("Percentage pricing has been retired. Use manual Normal, Elite, Professional, Store, and Public offer prices.", "info")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/admin/services/<service_id>/profit/customer", methods=["POST"])
def set_customer_profit_for_service(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    flash("Customer percentage overrides have been retired. Set the customer's level and use manual level prices instead.", "info")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/admin/services/<service_id>/profit/customer/<customer_id>/delete", methods=["POST"])
def delete_customer_profit_for_service(service_id, customer_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        s_id = ObjectId(service_id)
        c_id = ObjectId(customer_id)
    except Exception:
        flash("Invalid id(s).", "danger")
        return redirect(url_for("admin_services.manage_services"))

    res = service_profits_col.delete_one({"service_id": s_id, "customer_id": c_id})
    if res.deleted_count:
        flash("Customer profit override removed.", "info")
    else:
        flash("Override not found.", "warning")
    return redirect(url_for("admin_services.manage_services"))

@admin_services_bp.route("/api/services/<service_id>/profit", methods=["GET"])
def get_effective_profit(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    return jsonify({"success": True, "profit_percent": 0.0, "pricing_model": "manual"})

@admin_services_bp.route("/api/pricing/quote", methods=["GET"])
def quote_price():
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    amount = _to_float(request.args.get("amount"))
    if amount is None:
        return jsonify({"success": False, "error": "amount is required"}), 400
    q = {"amount": round(amount, 2), "profit": 0.0, "total": round(amount, 2), "profit_percent": 0.0}
    return jsonify({"success": True, "data": q})

@admin_services_bp.route("/admin/services/<service_id>/type", methods=["POST"])
def set_service_type(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    desired_raw = request.form.get("type")
    if desired_raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        desired_raw = payload.get("type")
    desired = _norm_type(desired_raw)

    if not desired:
        return jsonify({"success": False, "error": "type must be 'API' or 'OFF'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"type": desired, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "type": desired})

def _norm_status_flag(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"open", "1", "true", "on", "yes"}:
        return "OPEN"
    if s in {"closed", "0", "false", "off", "no"}:
        return "CLOSED"
    return None

def _norm_availability_flag(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"available", "in_stock", "instock", "1", "true", "on", "yes"}:
        return "AVAILABLE"
    if s in {"out_of_stock", "outofstock", "oos", "unavailable", "0", "false", "off", "no"}:
        return "OUT_OF_STOCK"
    return None

def _norm_display_flag(v: str | None) -> str | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in {"on", "display", "visible", "show", "shown", "1", "true", "yes"}:
        return "ON"
    if s in {"off", "hidden", "hide", "0", "false", "no"}:
        return "OFF"
    return None

@admin_services_bp.route("/admin/services/<service_id>/status", methods=["POST"])
def set_service_status(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("status")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("status")

    status_val = _norm_status_flag(raw)
    if not status_val:
        return jsonify({"success": False, "error": "status must be 'OPEN' or 'CLOSED'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"status": status_val, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "status": status_val})

@admin_services_bp.route("/admin/services/<service_id>/availability", methods=["POST"])
def set_service_availability(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("availability")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("availability")

    avail_val = _norm_availability_flag(raw)
    if not avail_val:
        return jsonify({"success": False, "error": "availability must be 'AVAILABLE' or 'OUT_OF_STOCK'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"availability": avail_val, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "availability": avail_val})

@admin_services_bp.route("/admin/services/<service_id>/display", methods=["POST"])
def set_service_display(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("display")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("display")

    display_val = _norm_display_flag(raw)
    if not display_val:
        return jsonify({"success": False, "error": "display must be 'ON' or 'OFF'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"display": display_val, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "display": display_val})


@admin_services_bp.route("/admin/services/<service_id>/store-display", methods=["POST"])
def set_service_store_display(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("store_display")
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("store_display")

    display_val = _norm_display_flag(raw)
    if not display_val:
        return jsonify({"success": False, "error": "store_display must be 'ON' or 'OFF'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"store_display": display_val, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "store_display": display_val})


@admin_services_bp.route("/admin/services/<service_id>/public-display", methods=["POST"])
def set_service_public_display(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("public_display")
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("public_display")

    display_val = _norm_display_flag(raw)
    if not display_val:
        return jsonify({"success": False, "error": "public_display must be 'ON' or 'OFF'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"public_display": display_val, "updated_at": datetime.utcnow()}}
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "public_display": display_val})

@admin_services_bp.route("/admin/services/<service_id>/provider", methods=["POST"])
def set_service_provider(service_id):
    if not _require_admin():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    try:
        _id = ObjectId(service_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid service id"}), 400

    raw = request.form.get("provider")
    if raw is None and request.is_json:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("provider")

    provider_val = _norm_provider(raw)
    if not provider_val:
        return jsonify({"success": False, "error": "provider must be 'portal02', 'dataconnect', 'codecraft', 'datakazina', or 'skplug'"}), 400

    res = services_col.update_one(
        {"_id": _id},
        {"$set": {"provider": provider_val, "updated_at": datetime.utcnow()}},
    )
    if not res.matched_count:
        return jsonify({"success": False, "error": "Service not found"}), 404

    return jsonify({"success": True, "service_id": str(_id), "provider": provider_val})
