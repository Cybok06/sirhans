from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from db import db
from datetime import datetime, timedelta
import re

admin_transactions_bp = Blueprint("admin_transactions", __name__)

transactions_col = db["transactions"]
users_col = db["users"]
orders_col = db["orders"]


def _transaction_order_keys(txn):
    meta = txn.get("meta") or {}
    return {value for value in (txn.get("reference"), txn.get("payment_reference"),
                               txn.get("order_id"), meta.get("order_id"))
            if isinstance(value, str) and value}


def _order_keys(order):
    return {value for value in (order.get("order_id"), order.get("paystack_reference"),
                               order.get("payment_reference"),
                               (order.get("debug") or {}).get("bulk_group_order_id"))
            if isinstance(value, str) and value}


def _transaction_source(txn, orders):
    txn_type = (txn.get("type") or "").lower()
    if txn_type == "deposit":
        return "Account Deposit"
    meta = txn.get("meta") or {}
    records = [txn, meta, *orders]
    channels = {str(record.get(key) or "").lower()
                for record in records for key in ("source", "channel", "paid_from")
                if isinstance(record.get(key), str)}
    if (meta.get("store_checkout") or any(record.get("store_slug") for record in records)
            or channels & {"store_checkout", "store_web"} or txn_type == "store_withdrawal"):
        return "Store"
    if (meta.get("public_checkout")
            or channels & {"public_web", "index_page", "public_paystack", "paystack_inline"}):
        return "Index Page"
    if channels & {"customer_dashboard", "wallet"} or txn_type == "purchase":
        return "Customer Dashboard"
    if "arkesel_ussd" in channels:
        return "USSD"
    return "Other"


def _attach_order_details(transactions):
    """Resolve this page's order IDs and sources with a single order lookup."""
    references = set().union(*(_transaction_order_keys(txn) for txn in transactions))
    orders_by_key = {}
    if references:
        fields = ("order_id", "paystack_reference", "payment_reference", "debug.bulk_group_order_id")
        orders = orders_col.find({"$or": [{field: {"$in": list(references)}} for field in fields]},
                                 {field: 1 for field in (*fields, "store_slug", "channel", "source", "paid_from")})
        for order in orders:
            for key in _order_keys(order):
                orders_by_key.setdefault(key, []).append(order)
    for txn in transactions:
        linked = {}
        for key in _transaction_order_keys(txn):
            for order in orders_by_key.get(key, []):
                linked[order["_id"]] = order
        orders = list(linked.values())
        ids = {order["order_id"] for order in orders if order.get("order_id")}
        meta = txn.get("meta") or {}
        ids.update(value for value in (txn.get("order_id"), meta.get("order_id"))
                   if isinstance(value, str) and value)
        if not ids and re.fullmatch(r"HAN\d+(?:-\d+)?", txn.get("reference") or "", re.I):
            ids.add(txn["reference"])
        txn["order_ids"] = sorted(ids) if txn.get("type") != "deposit" else []
        txn["source_label"] = _transaction_source(txn, orders)


@admin_transactions_bp.route("/admin/transactions")
def admin_view_transactions():
    # Auth
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    customer_id = (request.args.get("customer") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    order_id = (request.args.get("order_id") or "").strip()

    # pagination
    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    page = max(page, 1)

    per_page = 10

    query = {}

    if order_id:
        # An order lookup identifies a payment across all accounts and dates.
        # Stale filters must not hide guest payments or older orders.
        customer_id = start_date = end_date = ""
        exact_id = {"$regex": "^" + re.escape(order_id) + "$", "$options": "i"}
        references = {order_id}
        for order in orders_col.find({"order_id": exact_id}, {
            "order_id": 1, "paystack_reference": 1, "payment_reference": 1,
            "debug.bulk_group_order_id": 1,
        }):
            references.update(value for value in (
                order.get("order_id"), order.get("paystack_reference"),
                order.get("payment_reference"), (order.get("debug") or {}).get("bulk_group_order_id")
            ) if isinstance(value, str) and value)
        # Public payments use the gateway reference; wallet/store payments
        # normally use the order ID (or the primary ID of a split order).
        query["$or"] = [
            {"reference": {"$in": list(references)}},
            {"reference": exact_id},
            {"order_id": exact_id},
            {"meta.order_id": exact_id},
        ]

    # Filter by customer
    if customer_id:
        try:
            query["user_id"] = ObjectId(customer_id)
        except Exception:
            flash("Invalid customer selected.", "warning")

    # Date range filter (verified_at)
    # - start_date: >= start_dt 00:00
    # - end_date: <= end_dt 23:59:59 by using end_dt + 1 day (exclusive upper bound)
    verified_filter = {}
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            verified_filter["$gte"] = start_dt
        except Exception:
            flash("Invalid start date.", "warning")

    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            verified_filter["$lt"] = end_dt + timedelta(days=1)
        except Exception:
            flash("Invalid end date.", "warning")

    if verified_filter:
        query["verified_at"] = verified_filter

    # Count
    total_txns = transactions_col.count_documents(query)
    total_pages = max((total_txns + per_page - 1) // per_page, 1)

    # Clamp page to range (prevents dead pages when filters reduce results)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    # Fetch transactions
    transactions = list(
        transactions_col.find(query)
        .sort("verified_at", -1)
        .skip(skip)
        .limit(per_page)
    )

    # Load customers for dropdown
    customers = list(users_col.find({"role": "customer"}).sort("first_name", 1))

    # Attach user info efficiently
    user_ids = [t.get("user_id") for t in transactions if t.get("user_id")]
    users_map = {}
    if user_ids:
        for u in users_col.find({"_id": {"$in": list(set(user_ids))}}):
            users_map[u["_id"]] = u

    _attach_order_details(transactions)
    for txn in transactions:
        txn["user"] = users_map.get(txn.get("user_id"), {}) or {}

    return render_template(
        "admin_transactions.html",
        transactions=transactions,
        customers=customers,
        selected_customer=customer_id,
        order_id=order_id,
        start_date=start_date,
        end_date=end_date,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )
