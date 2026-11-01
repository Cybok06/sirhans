from __future__ import annotations

import math
import re
from datetime import datetime
from urllib.parse import urlencode

from bson import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from agent_code_utils import (
    agent_codes_col,
    generate_missing_agent_codes,
    regenerate_agent_code,
)
from db import db

admin_agent_codes_bp = Blueprint("admin_agent_codes", __name__)
users_col = db["users"]

AGENT_LEVELS = {"normal", "elite", "professional"}


def _require_admin():
    return session.get("role") == "admin"


def _to_object_id(value: str):
    try:
        return ObjectId(value)
    except Exception:
        return None


def _agent_level(value):
    level = (value or "normal").strip().lower()
    return level if level in AGENT_LEVELS else "normal"


@admin_agent_codes_bp.route("/admin/agent-codes")
def agent_codes_page():
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip()
    agent_level = (request.args.get("agent_level") or "").strip().lower()
    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 20

    conditions = [
        {"role": "customer"},
        {"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]},
    ]

    if q:
        regex = {"$regex": re.escape(q), "$options": "i"}
        matching_code_user_ids = [
            doc["user_id"]
            for doc in agent_codes_col.find({"agent_code": regex}, {"user_id": 1})
            if doc.get("user_id")
        ]
        conditions.append(
            {
                "$or": [
                    {"first_name": regex},
                    {"last_name": regex},
                    {"username": regex},
                    {"email": regex},
                    {"phone": regex},
                    {"business_name": regex},
                    {"_id": {"$in": matching_code_user_ids}},
                ]
            }
        )

    if agent_level in AGENT_LEVELS:
        if agent_level == "normal":
            conditions.append(
                {
                    "$or": [
                        {"agent_level": "normal"},
                        {"agent_level": {"$exists": False}},
                        {"agent_level": ""},
                        {"agent_level": None},
                    ]
                }
            )
        else:
            conditions.append({"agent_level": agent_level})

    query = {"$and": conditions}
    total = users_col.count_documents(query)
    total_pages = max(math.ceil(total / per_page), 1)
    if page > total_pages:
        page = total_pages

    customers = list(
        users_col.find(query)
        .sort([("_id", -1)])
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    public_agent_code = agent_codes_col.find_one(
        {"type": "public", "status": "active"},
        sort=[("updated_at", -1), ("created_at", -1)],
    )

    user_ids = [customer["_id"] for customer in customers]
    code_docs = {
        doc["user_id"]: doc
        for doc in agent_codes_col.find({"user_id": {"$in": user_ids}})
        if doc.get("user_id")
    }

    agents = []
    for customer in customers:
        code_doc = code_docs.get(customer["_id"], {})
        agents.append(
            {
                "user": customer,
                "agent_code": code_doc.get("agent_code", "Not generated"),
                "code_created_at": code_doc.get("created_at"),
                "code_updated_at": code_doc.get("updated_at"),
                "agent_level": _agent_level(customer.get("agent_level")),
            }
        )

    qs = request.args.to_dict(flat=True)
    qs.pop("page", None)
    base_qs = urlencode(qs)

    return render_template(
        "admin_agent_codes.html",
        agents=agents,
        public_agent_code=public_agent_code,
        q=q,
        agent_level=agent_level,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        base_qs=base_qs,
        agent_levels=sorted(AGENT_LEVELS),
    )


@admin_agent_codes_bp.route("/admin/agent-codes/generate", methods=["POST"])
def generate_agent_codes():
    if not _require_admin():
        return redirect(url_for("login.login"))

    result = generate_missing_agent_codes()
    flash(
        f"Agent codes generated: {result['created']} created, {result['skipped']} already existed.",
        "success",
    )
    return redirect(url_for("admin_agent_codes.agent_codes_page"))


@admin_agent_codes_bp.route("/admin/agent-codes/update/<user_id>", methods=["POST"])
def update_agent_code_user(user_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    oid = _to_object_id(user_id)
    if not oid:
        flash("Invalid user id.", "danger")
        return redirect(url_for("admin_agent_codes.agent_codes_page"))

    user = users_col.find_one({"_id": oid, "role": "customer"})
    if not user:
        flash("Agent customer not found.", "danger")
        return redirect(url_for("admin_agent_codes.agent_codes_page"))

    level = (request.form.get("agent_level") or "").strip().lower()
    if level not in AGENT_LEVELS:
        flash("Invalid agent level.", "danger")
        return redirect(url_for("admin_agent_codes.agent_codes_page"))

    now = datetime.utcnow()
    users_col.update_one(
        {"_id": oid, "role": "customer"},
        {
            "$set": {
                "agent_level": level,
                "agent_level_updated_at": now,
                "updated_at": now,
            }
        },
    )

    if request.form.get("regenerate_code") == "1":
        regenerate_agent_code(oid, session.get("admin_id") or session.get("user_id"))
        flash("Agent level updated and code regenerated.", "success")
    else:
        flash("Agent level updated.", "success")

    return redirect(request.referrer or url_for("admin_agent_codes.agent_codes_page"))
