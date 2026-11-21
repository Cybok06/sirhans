"""Full-admin management of accounts with selected admin pages."""
import re
import secrets
from datetime import datetime
from bson import ObjectId
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from pymongo.errors import DuplicateKeyError
from werkzeug.security import generate_password_hash
from db import db
from admin_access import ADMIN_PAGES, PAGE_IDS, is_managed

admin_users_bp = Blueprint("admin_users", __name__)
users_col = db["users"]


@admin_users_bp.route("/admin/no-access")
def no_access():
    if not getattr(g, "admin_user", None):
        return redirect(url_for("login.login"))
    return render_template("admin_access_denied.html", landing=None)


@admin_users_bp.route("/admin/users", methods=["GET", "POST"])
def users_page():
    actor = getattr(g, "admin_user", None)
    if not actor:
        return redirect(url_for("login.login"))
    if is_managed(actor):
        abort(403)
    session.setdefault("users_csrf", secrets.token_urlsafe(32))
    form = {}
    error = None
    editing = None
    edit_id = request.args.get("edit")
    managed_filter = {"role": "admin", "$or": [{"managed_admin": True}, {"allowed_pages": {"$exists": True}}]}
    if edit_id:
        if not ObjectId.is_valid(edit_id):
            abort(404)
        editing = users_col.find_one({**managed_filter, "_id": ObjectId(edit_id)})
        if not editing:
            abort(404)
        form = editing
    if request.method == "POST":
        if not secrets.compare_digest(session["users_csrf"], request.form.get("csrf_token", "")):
            abort(400, "The form expired. Reload the page and try again.")
        action = request.form.get("action")
        target = None
        if action in {"update", "status"}:
            target_id = request.form.get("user_id", "")
            if not ObjectId.is_valid(target_id):
                abort(404)
            target = users_col.find_one({**managed_filter, "_id": ObjectId(target_id)})
            if not target:
                abort(404)
        now = datetime.utcnow()
        if action == "status":
            status = request.form.get("status")
            if status not in {"active", "blocked"}:
                abort(400)
            users_col.update_one({**managed_filter, "_id": target["_id"]}, {
                "$set": {"status": status, "updated_at": now, "updated_by": actor["_id"]},
                "$inc": {"access_version": 1},
            })
            flash("User enabled." if status == "active" else "User disabled and signed out.", "success")
            return redirect(url_for("admin_users.users_page"))
        if action not in {"create", "update"}:
            abort(400)
        form = {key: request.form.get(key, "").strip() for key in ("name", "username", "email")}
        form["email"] = form["email"].lower()
        pages = list(dict.fromkeys(request.form.getlist("allowed_pages")))
        form["allowed_pages"] = pages
        editing = target
        password = request.form.get("password", "")
        if not form["name"] or len(form["name"]) > 120:
            error = "Enter a name of up to 120 characters."
        elif not re.fullmatch(r"[A-Za-z0-9._-]{3,64}", form["username"]):
            error = "Username must be 3–64 letters, numbers, dots, underscores or hyphens."
        elif len(form["email"]) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", form["email"]):
            error = "Enter a valid email address."
        elif not pages or not set(pages) <= PAGE_IDS:
            error = "Select at least one valid page."
        elif (action == "create" or password) and not 8 <= len(password) <= 256:
            error = "Use a password between 8 and 256 characters."
        elif password != request.form.get("confirm_password", ""):
            error = "Passwords do not match."
        else:
            duplicate = {"$or": [
                {"username": {"$regex": "^" + re.escape(form["username"]) + "$", "$options": "i"}},
                {"email": {"$regex": "^" + re.escape(form["email"]) + "$", "$options": "i"}},
            ]}
            if target:
                duplicate["_id"] = {"$ne": target["_id"]}
            if users_col.find_one(duplicate):
                error = "That username or email is already in use."
        if not error:
            data = {**form, "managed_admin": True, "updated_at": now, "updated_by": actor["_id"]}
            if password:
                data["password"] = generate_password_hash(password)
            try:
                if target:
                    update = {"$set": data}
                    if password:
                        update["$inc"] = {"access_version": 1}
                    users_col.update_one({**managed_filter, "_id": target["_id"]}, update)
                else:
                    users_col.insert_one({**data, "role": "admin", "status": "active", "created_at": now,
                                          "created_by": actor["_id"], "access_version": 0})
            except DuplicateKeyError:
                error = "That username or email is already in use."
            else:
                flash("User updated. Page access takes effect immediately." if target else "User created. They can now sign in with their username and password.", "success")
                return redirect(url_for("admin_users.users_page"))
    accounts = list(users_col.find(managed_filter, {"password": 0}).sort("created_at", -1))
    return render_template("admin_users.html", accounts=accounts, pages=ADMIN_PAGES,
                           form=form, editing=editing, error=error), (400 if error else 200)
