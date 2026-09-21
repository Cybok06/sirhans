"""Shared page catalog and request-time access control for managed admins."""
from bson import ObjectId
from flask import abort, g, jsonify, redirect, render_template, request, session, url_for
from pymongo.errors import PyMongoError
from db import db


# A page grants its existing actions as well as its view. Account administration
# stays with full administrators and is deliberately absent from this catalog.
ADMIN_PAGES = [
    ("dashboard", "Dashboard", "admin_dashboard.admin_dashboard", "speedometer2"),
    ("customers", "Customers", "admin_customers.view_customers", "people-fill"),
    ("agent_codes", "Agent Codes", "admin_agent_codes.agent_codes_page", "person-badge"),
    ("services", "Services", "admin_services.manage_services", "collection"),
    ("orders", "Orders", "admin_orders.admin_view_orders", "bag-check-fill"),
    ("phone_numbers", "Phone Numbers", "admin_phone_numbers.phone_numbers_page", "telephone-x"),
    ("announcements", "Announcements", "admin_announcements.admin_announcements", "megaphone-fill"),
    ("stores", "Stores", "admin_store.admin_stores_page", "shop-window"),
    ("transactions", "Transactions", "admin_transactions.admin_view_transactions", "cash-stack"),
    ("paystack_topups", "Paystack Top Ups", "admin_paystack_topups.admin_paystack_topups", "wallet-fill"),
    ("referrals", "Referrals", "admin_referrals.admin_referrals", "person-lines-fill"),
    ("balances", "Balances", "admin_balance.view_balances", "wallet2"),
    ("shares", "Shares / Wallet", "shares.shares_dashboard", "graph-up-arrow"),
    ("results_checker", "Results Checker", "admin_wassce_checker.admin_wassce_checker", "book"),
    ("purchases", "All Purchases", "admin_purchases.view_all_purchases", "basket2"),
    ("complaints", "Complaints", "admin_complaints.admin_view_complaints", "chat-dots"),
    ("login_logs", "Login Logs", "login_logs.view_login_logs", "shield-lock"),
    ("afa", "AFA", "admin_afa.admin_afa_page", "person-vcard"),
    ("settings", "Settings", "settings.manage_api", "gear"),
]
PAGE_IDS = {page[0] for page in ADMIN_PAGES}
BLUEPRINT_PAGES = {page[2].split('.')[0]: page[0] for page in ADMIN_PAGES}


def is_managed(user):
    return bool(user.get("managed_admin")) or "allowed_pages" in user


def allowed_pages(user):
    pages = user.get("allowed_pages", [])
    return set(pages) & PAGE_IDS if isinstance(pages, list) and all(isinstance(p, str) for p in pages) else set()


def landing_endpoint(user):
    if not is_managed(user):
        return "admin_dashboard.admin_dashboard"
    permitted = allowed_pages(user)
    return next((page[2] for page in ADMIN_PAGES if page[0] in permitted), "admin_users.no_access")


def can_access_page(page):
    user = getattr(g, "admin_user", None)
    return bool(user and (not is_managed(user) or page in allowed_pages(user)))


def can_manage_providers():
    user = getattr(g, "admin_user", None)
    return bool(user and user.get("role") == "admin" and not is_managed(user))


def init_admin_access(app):
    @app.before_request
    def enforce_admin_access():
        if session.get("role") != "admin":
            return
        # Logout must remain available even during a database outage.
        if request.endpoint in {"static", "login.logout"}:
            return
        try:
            uid = ObjectId(session.get("user_id", ""))
        except Exception:
            session.clear()
            return redirect(url_for("login.login"))
        try:
            user = db["users"].find_one({"_id": uid, "role": "admin"})
        except PyMongoError:
            abort(503)
        if (not user or user.get("deleted") or user.get("is_blocked")
                or (user.get("status") or "active").lower() != "active"
                or session.get("access_version", 0) != user.get("access_version", 0)):
            session.clear()
            return redirect(url_for("login.login"))
        g.admin_user = user
        if not is_managed(user):
            return
        if request.endpoint in {"login.login", "admin_users.no_access", "uploaded_file", "healthz"}:
            return
        if request.path in {"/", "/admin", "/admin/"}:
            return redirect(url_for(landing_endpoint(user)))
        if request.endpoint is None:
            return  # Preserve Flask's 404/405 without executing a view.
        page = BLUEPRINT_PAGES.get(request.blueprint)
        if request.endpoint == "reset.admin_generate_reset":
            page = "customers"
        if request.endpoint in {"customer_store.api_admin_store_withdraw_requests", "customer_store.api_admin_store_withdraw_update_status"}:
            page = "dashboard"
        if page and page in allowed_pages(user):
            return
        if request.is_json or request.accept_mimetypes.best == "application/json":
            return jsonify(status="error", message="You do not have access to this page."), 403
        return render_template("admin_access_denied.html", landing=landing_endpoint(user)), 403

    @app.context_processor
    def inject_admin_access():
        return {"can_access_page": can_access_page, "admin_page_catalog": ADMIN_PAGES,
                "can_manage_providers": can_manage_providers}
