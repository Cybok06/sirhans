from __future__ import annotations

from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from announcement_utils import (
    TARGETS,
    TONE_STYLES,
    announcements_col,
    delete_announcement,
    normalize_targets,
    serialize_announcement,
)

admin_announcements_bp = Blueprint("admin_announcements", __name__)


def _is_admin() -> bool:
    return bool(session.get("admin_logged_in") and session.get("role") == "admin")


@admin_announcements_bp.route("/admin/announcements", methods=["GET", "POST"])
def admin_announcements():
    if not _is_admin():
        return redirect(url_for("login.login"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip().lower()
        admin_name = (session.get("username") or session.get("name") or "Admin").strip()

        if action == "create":
            title = (request.form.get("title") or "").strip()
            message = (request.form.get("message") or "").strip()
            tone = (request.form.get("tone") or "info").strip().lower()
            targets = normalize_targets(request.form.getlist("targets"))

            if not title:
                flash("Announcement title is required.", "danger")
                return redirect(url_for("admin_announcements.admin_announcements"))
            if not message:
                flash("Announcement message is required.", "danger")
                return redirect(url_for("admin_announcements.admin_announcements"))
            if not targets:
                flash("Select at least one display location.", "danger")
                return redirect(url_for("admin_announcements.admin_announcements"))
            if tone not in TONE_STYLES:
                tone = "info"

            now = datetime.utcnow()
            announcements_col.insert_one(
                {
                    "title": title,
                    "message": message,
                    "tone": tone,
                    "targets": targets,
                    "is_active": True,
                    "created_at": now,
                    "updated_at": now,
                    "created_by": session.get("user_id"),
                    "created_by_name": admin_name,
                }
            )
            flash("Announcement published successfully.", "success")
            return redirect(url_for("admin_announcements.admin_announcements"))

        if action == "delete":
            announcement_id = (request.form.get("announcement_id") or "").strip()
            if delete_announcement(announcement_id, deleted_by=admin_name):
                flash("Announcement deleted.", "success")
            else:
                flash("Announcement could not be deleted.", "warning")
            return redirect(url_for("admin_announcements.admin_announcements"))

        flash("Unknown announcement action.", "danger")
        return redirect(url_for("admin_announcements.admin_announcements"))

    docs = list(
        announcements_col.find({"deleted_at": {"$exists": False}}).sort(
            [("created_at", -1), ("_id", -1)]
        )
    )
    announcements = [serialize_announcement(doc) for doc in docs]

    stats = {
        "total": len(announcements),
        "active": sum(1 for item in announcements if item.get("is_active")),
        "index_page": sum(1 for item in announcements if "index_page" in (item.get("targets") or [])),
        "customer_dashboard": sum(
            1 for item in announcements if "customer_dashboard" in (item.get("targets") or [])
        ),
        "store_page": sum(1 for item in announcements if "store_page" in (item.get("targets") or [])),
    }

    return render_template(
        "admin_announcements.html",
        announcements=announcements,
        stats=stats,
        target_options=TARGETS,
        tone_options=TONE_STYLES,
    )
