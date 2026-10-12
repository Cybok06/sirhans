"""Shared page catalog and request-time access control for managed admins."""
import re

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
    ("failed_receipts", "Failed/No receipt", "failed_receipts.admin_page", "receipt-cutoff"),
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


# Presentation only: never replace the provider IDs stored in the database.
PROVIDER_CODES = {
    "bundleportal": "BNP", "portal02": "PT2", "dataconnect": "DTC",
    "datakazina": "DTK", "datakazina_mtn": "DTK", "datakazina_mtn_express": "DTK-E", "codecraft": "CDK", "skplug": "SKP",
}
_PROVIDER_NAME_RE = re.compile(
    r"bundle[\s_-]*portal(?:[\s_-]*api)?|portal[\s_-]*0*2|"
    r"data[\s_-]*connect|dconnect|data[\s_-]*kazina(?:[\s_-]*mtn(?:[\s_-]*express)?)?|dakazina|"
    r"code[\s_-]*craft|sk[\s_-]*plug", re.I,
)


def uses_provider_codes():
    user = getattr(g, "admin_user", None)
    return bool(user and is_managed(user))


def _provider_code(value):
    key = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    if key.startswith("bundleportal"):
        return "BNP"
    key = {"portal2": "portal02", "portal002": "portal02",
           "dconnect": "dataconnect", "dakazina": "datakazina"}.get(key, key)
    if key == "datakazinamtn":
        return PROVIDER_CODES["datakazina_mtn"]
    if key == "datakazinamtnexpress":
        return PROVIDER_CODES["datakazina_mtn_express"]
    return PROVIDER_CODES.get(key)


def provider_display_name(value, full_name=None):
    if uses_provider_codes():
        # Unknown providers must not expose a new supplier's name by default.
        return _provider_code(value) or ("Provider" if value else "")
    return str(value or "") if full_name is None else full_name


def provider_badge_class(value):
    # Neutral class names keep supplier names out of managed users' HTML/CSS.
    return {"CDK": "prov-1", "DTC": "prov-2", "PT2": "prov-3",
            "DTK": "prov-4", "DTK-E": "prov-4", "SKP": "prov-5"}.get(_provider_code(value), "")


def provider_status_text(value):
    """Mask known supplier names in provider-generated status/error text."""
    if not uses_provider_codes():
        return value
    if isinstance(value, dict):
        return {key: provider_status_text(item) for key, item in value.items()}
    if isinstance(value, list):
        return [provider_status_text(item) for item in value]
    if isinstance(value, str):
        return _PROVIDER_NAME_RE.sub(lambda match: _provider_code(match.group()) or "Provider", value)
    return value


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
                "can_manage_providers": can_manage_providers,
                "provider_display_name": provider_display_name,
                "provider_badge_class": provider_badge_class,
                "provider_status_text": provider_status_text}
