from datetime import datetime
from io import BytesIO
import re

import pandas as pd
from flask import Blueprint, redirect, render_template, request, send_file, session, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from db import db

admin_phone_numbers_bp = Blueprint("admin_phone_numbers", __name__)

orders_col = db["orders"]
users_col = db["users"]
blocked_phone_numbers_col = db["blocked_phone_numbers"]
known_number_attempts_col = db["known_number_attempts"]
services_col = db["services"]
settings_col = db["settings"]

EXPORT_SOURCES = {
    "store": "Store",
    "ussd": "USSD",
    "index": "Index Page",
    "agents": "Agents / Customers",
}
ATTEMPT_SOURCE_LABELS = {
    "store": "Store",
    "index": "Index Page",
    "customer_dashboard": "Customer Dashboard",
}


def _require_admin():
    return session.get("role") == "admin"


def _normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) == 12:
        return f"0{digits[3:]}"
    if len(digits) == 9:
        return f"0{digits}"
    return digits


def _pagination_params():
    page_raw = request.args.get("page", 1)
    try:
        page = max(int(page_raw), 1)
    except Exception:
        page = 1
    per_page = 50
    return page, per_page


def _export_source(value: str) -> str:
    source = (value or "").strip().lower()
    return source if source in EXPORT_SOURCES else ""


def _export_sources() -> list[str]:
    raw_values = request.args.getlist("source")
    if not raw_values:
        raw_values = [request.args.get("source") or ""]

    sources: list[str] = []
    for raw in raw_values:
        for part in str(raw or "").split(","):
            source = _export_source(part)
            if source and source not in sources:
                sources.append(source)
    return sources or list(EXPORT_SOURCES.keys())


def _normalize_text(value) -> str:
    return " ".join(str(value or "").strip().split())


def _known_number_enforcement_enabled() -> bool:
    try:
        doc = settings_col.find_one({"_id": "checkout_controls"}, {"enforce_known_number_check": 1})
    except Exception:
        doc = None
    return bool((doc or {}).get("enforce_known_number_check", True))


def _normalize_network_name(value) -> str:
    text = _normalize_text(value).lower()
    if not text:
        return ""
    if "mtn" in text:
        return "MTN"
    if "telecel" in text or "vodafone" in text:
        return "Telecel"
    if (
        "airteltigo" in text
        or "airtel tigo" in text
        or "airtel-tigo" in text
        or "i share" in text
        or "ishare" in text
        or text == "at"
    ):
        return "AirtelTigo"
    return text.upper()


def _guess_network_from_service_name(service_name) -> str:
    return _normalize_network_name(service_name)


def _build_service_network_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    try:
        docs = services_col.find({}, {"name": 1, "provider_network": 1, "network": 1, "service_network": 1})
        for doc in docs:
            name = _normalize_text(doc.get("name"))
            if not name:
                continue
            network = (
                _normalize_network_name(doc.get("provider_network"))
                or _normalize_network_name(doc.get("service_network"))
                or _normalize_network_name(doc.get("network"))
                or _guess_network_from_service_name(name)
            )
            if network:
                mapping[name.casefold()] = network
    except Exception:
        pass
    return mapping


def _export_network_options() -> list[str]:
    service_network_map = _build_service_network_map()
    networks: list[str] = []
    seen = set()
    for network in service_network_map.values():
        if network and network not in seen:
            seen.add(network)
            networks.append(network)
    for fallback in ("MTN", "Telecel", "AirtelTigo"):
        if fallback not in seen:
            networks.append(fallback)
    return networks


def _selected_export_network() -> str:
    selected = _normalize_network_name(request.args.get("network"))
    return selected if selected and selected != "ANY" else ""


def _resolve_item_network(item: dict, service_network_map: dict[str, str]) -> str:
    return (
        _normalize_network_name(item.get("provider_network"))
        or _normalize_network_name(item.get("network"))
        or _normalize_network_name(item.get("network_name"))
        or service_network_map.get(_normalize_text(item.get("serviceName")).casefold(), "")
        or _guess_network_from_service_name(item.get("serviceName"))
    )


def _source_match(source: str) -> dict:
    if source == "store":
        return {
            "$and": [
                {
                    "$or": [
                        {"store_slug": {"$exists": True, "$nin": [None, ""]}},
                        {"debug.store_checkout": True},
                        {"channel": {"$in": ["store_web", "store_checkout"]}},
                    ]
                },
                {"channel": {"$ne": "arkesel_ussd"}},
                {"source.type": {"$ne": "ussd"}},
            ]
        }
    if source == "ussd":
        return {
            "$or": [
                {"channel": "arkesel_ussd"},
                {"source.provider": "arkesel"},
                {"source.type": "ussd"},
            ]
        }
    if source == "index":
        return {
            "$and": [
                {"paid_from": "public_paystack"},
                {"channel": {"$ne": "arkesel_ussd"}},
                {"source.type": {"$ne": "ussd"}},
                {"store_slug": {"$in": [None, ""]}},
            ]
        }
    return {}


def _apply_block_status(rows: list[dict]) -> list[dict]:
    row_keys = [_normalize_phone(r.get("phone")) for r in rows if r.get("phone")]
    active_blocks = list(
        blocked_phone_numbers_col.find(
            {"is_active": True, "normalized_phone": {"$in": row_keys}},
            {"normalized_phone": 1, "reason": 1, "_id": 0},
        )
    )
    blocked_map = {d.get("normalized_phone"): d for d in active_blocks if d.get("normalized_phone")}

    for row in rows:
        key = _normalize_phone(row.get("phone"))
        row["normalized_phone"] = key
        row["is_blocked"] = key in blocked_map
        row["block_reason"] = (blocked_map.get(key) or {}).get("reason", "")
    return rows


def _apply_known_number_override_status(rows: list[dict]) -> list[dict]:
    row_keys = [_normalize_phone(r.get("phone")) for r in rows if r.get("phone")]
    allowed_rows = list(
        known_number_attempts_col.find(
            {"normalized_phone": {"$in": row_keys}, "is_allowed_override": True},
            {"normalized_phone": 1, "_id": 0},
        )
    )
    allowed_map = {d.get("normalized_phone"): True for d in allowed_rows if d.get("normalized_phone")}

    for row in rows:
        key = row.get("normalized_phone") or _normalize_phone(row.get("phone"))
        row["is_allowed_override"] = bool(allowed_map.get(key))
    return rows


def _fetch_phone_rows(q: str, skip: int | None = None, limit: int | None = None, source: str = ""):
    if q:
        phone_match = {"$regex": re.escape(q), "$options": "i"}
    else:
        phone_match = {"$exists": True, "$nin": [None, ""]}

    base_match = {"items.phone": phone_match, **_source_match(source)}

    total_pipeline = [
        {"$unwind": "$items"},
        {"$match": base_match},
        {"$group": {"_id": "$items.phone"}},
        {"$count": "total"},
    ]
    total_agg = list(orders_col.aggregate(total_pipeline))
    total = int(total_agg[0]["total"]) if total_agg else 0

    if limit is not None and int(limit) <= 0:
        return total, []

    pipeline = [
        {"$unwind": "$items"},
        {"$match": base_match},
        {
            "$group": {
                "_id": "$items.phone",
                "order_ids": {"$addToSet": "$order_id"},
                "last_order_at": {"$max": "$created_at"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "phone": "$_id",
                "orders_count": {"$size": "$order_ids"},
                "last_order_at": 1,
            }
        },
        {"$sort": {"orders_count": 1, "phone": 1, "last_order_at": -1}},
    ]
    if skip is not None:
        pipeline.append({"$skip": skip})
    if limit is not None and int(limit) > 0:
        pipeline.append({"$limit": limit})

    rows = list(orders_col.aggregate(pipeline))

    rows = _apply_block_status(rows)
    rows = _apply_known_number_override_status(rows)

    return total, rows


def _fetch_not_in_database_rows(q: str) -> list[dict]:
    query: dict = {}
    if q:
        query["$or"] = [
            {"phone": {"$regex": re.escape(q), "$options": "i"}},
            {"normalized_phone": {"$regex": re.escape(q), "$options": "i"}},
        ]

    rows: list[dict] = []
    for doc in known_number_attempts_col.find(
        query,
        {
            "phone": 1,
            "normalized_phone": 1,
            "attempt_count": 1,
            "first_seen_at": 1,
            "last_seen_at": 1,
            "sources": 1,
            "last_service_name": 1,
        },
    ):
        phone = doc.get("phone") or doc.get("normalized_phone") or ""
        normalized_phone = _normalize_phone(phone)
        if not normalized_phone:
            continue
        rows.append(
            {
                "phone": phone,
                "normalized_phone": normalized_phone,
                "orders_count": 0,
                "last_order_at": doc.get("last_seen_at") or doc.get("first_seen_at"),
                "attempt_count": int(doc.get("attempt_count") or 0),
                "attempt_sources": list(doc.get("sources") or []),
                "last_service_name": doc.get("last_service_name") or "",
                "is_not_in_database": True,
            }
        )
    return rows


def _source_labels(values: list[str]) -> str:
    labels = [ATTEMPT_SOURCE_LABELS.get(v, _normalize_text(v).title()) for v in values if v]
    return ", ".join(labels)


def _fetch_phone_page_rows(q: str) -> list[dict]:
    _, order_rows = _fetch_phone_rows(q=q, skip=None, limit=None)
    attempt_rows = _fetch_not_in_database_rows(q=q)

    merged: dict[str, dict] = {}

    for row in order_rows:
        key = row.get("normalized_phone") or _normalize_phone(row.get("phone"))
        if not key:
            continue
        merged[key] = {
            **row,
            "attempt_count": 0,
            "attempt_sources": [],
            "last_service_name": "",
            "is_not_in_database": False,
        }

    for row in attempt_rows:
        key = row.get("normalized_phone") or _normalize_phone(row.get("phone"))
        if not key:
            continue
        existing = merged.get(key)
        if existing:
            existing["attempt_count"] = max(int(existing.get("attempt_count") or 0), int(row.get("attempt_count") or 0))
            existing["attempt_sources"] = list(dict.fromkeys([*(existing.get("attempt_sources") or []), *(row.get("attempt_sources") or [])]))
            if row.get("last_service_name") and not existing.get("last_service_name"):
                existing["last_service_name"] = row.get("last_service_name")
            existing["is_not_in_database"] = bool(int(existing.get("orders_count") or 0) <= 0 and int(row.get("attempt_count") or 0) > 0)
            continue
        merged[key] = row

    rows = list(merged.values())
    rows = _apply_block_status(rows)
    rows = _apply_known_number_override_status(rows)
    for row in rows:
        row["is_not_in_database"] = bool((not row.get("is_blocked")) and (not row.get("is_allowed_override")) and int(row.get("orders_count") or 0) <= 0 and int(row.get("attempt_count") or 0) > 0)
        row["attempt_sources_label"] = _source_labels(row.get("attempt_sources") or [])

    rows.sort(
        key=lambda r: (
            0 if r.get("is_blocked") else 1 if r.get("is_not_in_database") else 2 if r.get("is_allowed_override") else 3,
            int(r.get("orders_count") or 0),
            str(r.get("phone") or ""),
            r.get("last_order_at") or datetime.min,
        )
    )
    return rows


def _fetch_agent_phone_rows(q: str) -> list[dict]:
    phone_match = {"$exists": True, "$nin": [None, ""]}
    conditions = [
        {"role": "customer"},
        {"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]},
        {"phone": phone_match},
    ]
    if q:
        regex = {"$regex": re.escape(q), "$options": "i"}
        conditions.append(
            {
                "$or": [
                    {"phone": regex},
                    {"phone_normalized": regex},
                    {"first_name": regex},
                    {"last_name": regex},
                    {"username": regex},
                    {"business_name": regex},
                ]
            }
        )

    rows = []
    for user in users_col.find({"$and": conditions}, {"phone": 1, "created_at": 1}):
        phone = user.get("phone") or ""
        if not _normalize_phone(phone):
            continue
        rows.append(
            {
                "phone": phone,
                "orders_count": 0,
                "last_order_at": user.get("created_at"),
            }
        )
    return rows


def _fetch_source_export_rows(q: str, source: str, selected_network: str, service_network_map: dict[str, str]) -> list[dict]:
    if source == "agents":
        if selected_network:
            return []
        return _fetch_agent_phone_rows(q)

    if q:
        phone_match = {"$regex": re.escape(q), "$options": "i"}
    else:
        phone_match = {"$exists": True, "$nin": [None, ""]}

    pipeline = [
        {"$unwind": "$items"},
        {"$match": {"items.phone": phone_match, **_source_match(source)}},
        {
            "$project": {
                "_id": 0,
                "phone": "$items.phone",
                "last_order_at": "$created_at",
                "provider_network": "$items.provider_network",
                "network": "$items.network",
                "network_name": "$items.network_name",
                "serviceName": "$items.serviceName",
                "source_label": {"$literal": EXPORT_SOURCES.get(source, source.title())},
            }
        },
    ]

    grouped: dict[str, dict] = {}
    for row in orders_col.aggregate(pipeline):
        network_name = _resolve_item_network(row, service_network_map)
        if selected_network and network_name != selected_network:
            continue

        key = _normalize_phone(row.get("phone"))
        if not key:
            continue

        existing = grouped.setdefault(
            key,
            {
                "phone": row.get("phone") or key,
                "orders_count": 0,
                "last_order_at": None,
                "sources": [],
                "network": network_name,
            },
        )
        existing["orders_count"] += 1
        last = row.get("last_order_at")
        if last and (not existing.get("last_order_at") or last > existing["last_order_at"]):
            existing["last_order_at"] = last
        source_label = row.get("source_label")
        if source_label and source_label not in existing["sources"]:
            existing["sources"].append(source_label)
        if network_name and not existing.get("network"):
            existing["network"] = network_name

    return list(grouped.values())


def _merge_export_rows(q: str, sources: list[str], selected_network: str = "") -> list[dict]:
    service_network_map = _build_service_network_map()
    merged: dict[str, dict] = {}

    def put(row: dict, source: str) -> None:
        key = _normalize_phone(row.get("phone"))
        if not key:
            return
        existing = merged.setdefault(
            key,
            {
                "phone": row.get("phone") or key,
                "orders_count": 0,
                "last_order_at": None,
                "sources": [],
                "network": row.get("network") or "",
            },
        )
        existing["orders_count"] += int(row.get("orders_count") or 0)
        last = row.get("last_order_at")
        if last and (not existing.get("last_order_at") or last > existing["last_order_at"]):
            existing["last_order_at"] = last
        label = row.get("source_label") or EXPORT_SOURCES.get(source, source.title())
        if label not in existing["sources"]:
            existing["sources"].append(label)
        if row.get("network") and not existing.get("network"):
            existing["network"] = row.get("network")

    for source in sources:
        for row in _fetch_source_export_rows(q=q, source=source, selected_network=selected_network, service_network_map=service_network_map):
            put(row, source)

    rows = list(merged.values())
    rows.sort(key=lambda r: (int(r.get("orders_count") or 0), str(r.get("phone") or ""), r.get("last_order_at") or datetime.min))
    return _apply_block_status(rows)


@admin_phone_numbers_bp.route("/admin/phone-numbers")
def phone_numbers_page():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    page, per_page = _pagination_params()
    all_rows = _fetch_phone_page_rows(q=q)
    total = len(all_rows)
    total_pages = max((total + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page
    rows = all_rows[skip: skip + per_page]

    total_blocked = blocked_phone_numbers_col.count_documents({"is_active": True})
    total_not_in_database = sum(1 for row in all_rows if row.get("is_not_in_database"))

    return render_template(
        "admin_phone_numbers.html",
        rows=rows,
        q=q,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        total_blocked=total_blocked,
        total_not_in_database=total_not_in_database,
        enforce_known_number_check=_known_number_enforcement_enabled(),
        export_networks=_export_network_options(),
        selected_export_network=_selected_export_network(),
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/known-number-toggle", methods=["POST"])
def update_known_number_toggle():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.form.get("q") or "").strip()
    page = (request.form.get("page") or "1").strip()
    enabled = str(request.form.get("enforce_known_number_check") or "").strip().lower() in {"1", "true", "on", "yes"}
    now = datetime.utcnow()
    settings_col.update_one(
        {"_id": "checkout_controls"},
        {
            "$set": {
                "enforce_known_number_check": enabled,
                "updated_at": now,
                "updated_by": session.get("admin_id") or session.get("user_id"),
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return redirect(url_for("admin_phone_numbers.phone_numbers_page", q=q, page=page))


@admin_phone_numbers_bp.route("/admin/phone-numbers/allow-known-number", methods=["POST"])
def allow_known_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    page = (request.form.get("page") or "1").strip()
    key = _normalize_phone(phone)
    if key:
        now = datetime.utcnow()
        known_number_attempts_col.update_one(
            {"normalized_phone": key},
            {
                "$set": {
                    "phone": phone or key,
                    "normalized_phone": key,
                    "is_allowed_override": True,
                    "allowed_override_at": now,
                    "allowed_override_by": session.get("admin_id") or session.get("user_id"),
                    "updated_at": now,
                },
                "$setOnInsert": {"first_seen_at": now, "attempt_count": 0},
            },
            upsert=True,
        )
    return redirect(url_for("admin_phone_numbers.phone_numbers_page", q=q, page=page))


@admin_phone_numbers_bp.route("/admin/phone-numbers/revoke-known-number", methods=["POST"])
def revoke_known_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    page = (request.form.get("page") or "1").strip()
    key = _normalize_phone(phone)
    if key:
        known_number_attempts_col.update_one(
            {"normalized_phone": key},
            {
                "$set": {
                    "is_allowed_override": False,
                    "updated_at": datetime.utcnow(),
                    "allowed_override_revoked_by": session.get("admin_id") or session.get("user_id"),
                }
            },
        )
    return redirect(url_for("admin_phone_numbers.phone_numbers_page", q=q, page=page))


@admin_phone_numbers_bp.route("/admin/phone-numbers/export/excel")
def export_phone_numbers_excel():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    sources = _export_sources()
    selected_network = _selected_export_network()
    source_label = ", ".join(EXPORT_SOURCES.get(source, source.title()) for source in sources)
    rows = _merge_export_rows(q=q, sources=sources, selected_network=selected_network)
    generated_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    data = []
    if selected_network:
        column_name = f"{selected_network} Numbers"
        for row in rows:
            data.append({column_name: row.get("phone", "")})
        df = pd.DataFrame(data, columns=[column_name])
    else:
        for row in rows:
            data.append(
                {
                    "Phone Number": row.get("phone", ""),
                    "Orders Placed": int(row.get("orders_count") or 0),
                    "Status": "Blocked" if row.get("is_blocked") else "Active",
                    "Block Reason": row.get("block_reason") or "",
                    "Last Order At": (
                        row.get("last_order_at").strftime("%Y-%m-%d %H:%M")
                        if row.get("last_order_at")
                        else ""
                    ),
                    "Source": ", ".join(row.get("sources") or []) or source_label,
                    "Generated At": generated_at,
                }
            )
    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        sheet_name = f"{selected_network} Numbers" if selected_network else "Phone Numbers"
        df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
    output.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base_name = f"{selected_network}_numbers" if selected_network else "phone_numbers"
    suffix = ""
    if not selected_network and sources:
        suffix = "_" + "_".join(sources)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{base_name}{suffix}_{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/export/pdf")
def export_phone_numbers_pdf():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    sources = _export_sources()
    selected_network = _selected_export_network()
    source_label = ", ".join(EXPORT_SOURCES.get(source, source.title()) for source in sources)
    rows = _merge_export_rows(q=q, sources=sources, selected_network=selected_network)
    total = len(rows)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=24,
        leftMargin=24,
        topMargin=24,
        bottomMargin=24,
    )

    styles = getSampleStyleSheet()
    title_label = f"{selected_network} Numbers" if selected_network else f"Phone Numbers Report - {source_label}"
    title = Paragraph(title_label, styles["Title"])
    subtitle = Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')} | Total: {total}",
        styles["Normal"],
    )

    if selected_network:
        table_data = [["#", f"{selected_network} Numbers"]]
        for idx, row in enumerate(rows, start=1):
            table_data.append([str(idx), str(row.get("phone") or "")])
        tbl = Table(table_data, repeatRows=1, colWidths=[40, 260])
    else:
        table_data = [["#", "Phone Number", "Orders", "Source", "Status", "Reason", "Last Order"]]
        for idx, row in enumerate(rows, start=1):
            table_data.append(
                [
                    str(idx),
                    str(row.get("phone") or ""),
                    str(int(row.get("orders_count") or 0)),
                    ", ".join(row.get("sources") or []),
                    "Blocked" if row.get("is_blocked") else "Active",
                    str(row.get("block_reason") or ""),
                    row.get("last_order_at").strftime("%Y-%m-%d %H:%M") if row.get("last_order_at") else "-",
                ]
            )
        tbl = Table(table_data, repeatRows=1, colWidths=[32, 120, 54, 120, 64, 230, 100])

    tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (2, 0), (3, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )

    doc.build([title, Spacer(1, 8), subtitle, Spacer(1, 12), tbl])
    buffer.seek(0)

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    base_name = f"{selected_network}_numbers" if selected_network else "phone_numbers"
    suffix = ""
    if not selected_network and sources:
        suffix = "_" + "_".join(sources)
    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"{base_name}{suffix}_{stamp}.pdf",
        mimetype="application/pdf",
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/block", methods=["POST"])
def block_phone_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    page = (request.form.get("page") or "1").strip()
    reason = (request.form.get("reason") or "").strip()

    key = _normalize_phone(phone)
    if key:
        now = datetime.utcnow()
        blocked_phone_numbers_col.update_one(
            {"normalized_phone": key},
            {
                "$set": {
                    "phone": phone,
                    "normalized_phone": key,
                    "reason": reason,
                    "is_active": True,
                    "updated_at": now,
                    "blocked_by": session.get("admin_id") or session.get("user_id"),
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    return redirect(url_for("admin_phone_numbers.phone_numbers_page", q=q, page=page))


@admin_phone_numbers_bp.route("/admin/phone-numbers/unblock", methods=["POST"])
def unblock_phone_number():
    if not _require_admin():
        return redirect(url_for("login.login"))

    phone = (request.form.get("phone") or "").strip()
    q = (request.form.get("q") or "").strip()
    page = (request.form.get("page") or "1").strip()

    key = _normalize_phone(phone)
    if key:
        blocked_phone_numbers_col.update_one(
            {"normalized_phone": key, "is_active": True},
            {
                "$set": {
                    "is_active": False,
                    "updated_at": datetime.utcnow(),
                    "unblocked_by": session.get("admin_id") or session.get("user_id"),
                }
            },
        )

    return redirect(url_for("admin_phone_numbers.phone_numbers_page", q=q, page=page))
