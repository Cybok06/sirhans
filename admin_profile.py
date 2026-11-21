from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from werkzeug.security import generate_password_hash, check_password_hash
from bson import ObjectId
from datetime import datetime
from db import db
import re


admin_profile_bp = Blueprint("admin_profile", __name__)
users_col = db["users"]

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9._-]{3,}$")


@admin_profile_bp.route("/admin/profile", methods=["GET", "POST"])
def admin_profile():
    if not session.get("admin_logged_in") or session.get("role") != "admin":
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))

    admin = users_col.find_one({"_id": ObjectId(user_id), "role": "admin"})
    if not admin:
        flash("Admin account not found.", "danger")
        return redirect(url_for("login.login"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()

        if action == "update_username":
            new_username = (request.form.get("new_username") or "").strip()
            current_password = request.form.get("current_password") or ""

            if not new_username:
                flash("New username is required.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not _USERNAME_RE.fullmatch(new_username):
                flash("Invalid username format.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not check_password_hash(admin.get("password", ""), current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if users_col.find_one({"username": new_username, "_id": {"$ne": ObjectId(user_id)}}):
                flash("Username already exists.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))

            users_col.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"username": new_username, "updated_at": datetime.utcnow()}}
            )
            session["username"] = new_username
            flash("Username updated successfully.", "success")
            return redirect(url_for("admin_profile.admin_profile"))

        if action == "change_password":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            confirm_password = request.form.get("confirm_password") or ""

            if not check_password_hash(admin.get("password", ""), current_password):
                flash("Current password is incorrect.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not new_password or not confirm_password:
                flash("New password and confirmation are required.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if new_password != confirm_password:
                flash("New passwords do not match.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))

            users_col.update_one(
                {"_id": ObjectId(user_id)},
                {"$set": {"password": generate_password_hash(new_password), "updated_at": datetime.utcnow()}}
            )
            flash("Password updated successfully.", "success")
            return redirect(url_for("admin_profile.admin_profile"))

        if action == "create_admin":
            username = (request.form.get("username") or "").strip()
            name = (request.form.get("name") or "").strip()
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            confirm_password = request.form.get("confirm_password") or ""

            if not username or not name or not email or not password or not confirm_password:
                flash("All fields are required to create an admin.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if not _USERNAME_RE.fullmatch(username):
                flash("Invalid username format.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if password != confirm_password:
                flash("Passwords do not match.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if users_col.find_one({"username": username}):
                flash("Username already exists.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))
            if users_col.find_one({"email": email}):
                flash("Email already exists.", "danger")
                return redirect(url_for("admin_profile.admin_profile"))

            now = datetime.utcnow()
            users_col.insert_one({
                "username": username,
                "password": generate_password_hash(password),
                "role": "admin",
                "name": name,
                "email": email,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            })
            flash("New admin account created.", "success")
            return redirect(url_for("admin_profile.admin_profile"))

        flash("Unknown action.", "danger")
        return redirect(url_for("admin_profile.admin_profile"))

    return render_template("admin_profile.html", admin=admin)
