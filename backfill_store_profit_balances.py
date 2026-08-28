"""Backfill store profits skipped by the shared store checkout path.

Dry-run is the default. Pass --apply to update store_accounts and mark orders.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime

from db import db


STARTED_AT = datetime(2026, 8, 19, 17, 45)


def money(value) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    accounts = db["store_accounts"]
    orders = db["orders"]
    candidates = []
    query = {
        "created_at": {"$gte": STARTED_AT},
        "debug.shared_place_store_order": True,
        "payment_status": "paid",
    }

    for order in orders.find(query).sort("created_at", 1):
        slug = str(order.get("store_slug") or "").strip()
        order_id = str(order.get("order_id") or "").strip()
        profit = round(
            sum(money(item.get("store_profit_amount")) for item in (order.get("items") or [])),
            2,
        )
        if not slug or not order_id or profit <= 0:
            continue
        account = accounts.find_one(
            {"store_slug": slug}, {"credited_order_ids": 1}
        ) or {}
        if order_id in (account.get("credited_order_ids") or []):
            continue
        candidates.append((order, slug, order_id, profit))

    by_store = defaultdict(lambda: {"orders": 0, "profit": 0.0})
    credited_count = 0
    credited_total = 0.0

    for order, slug, order_id, profit in candidates:
        by_store[slug]["orders"] += 1
        by_store[slug]["profit"] = round(by_store[slug]["profit"] + profit, 2)
        if not args.apply:
            continue

        now = datetime.utcnow()
        accounts.update_one(
            {"store_slug": slug},
            {
                "$setOnInsert": {
                    "store_slug": slug,
                    "total_profit_balance": 0.0,
                    "created_at": now,
                }
            },
            upsert=True,
        )
        result = accounts.update_one(
            {"store_slug": slug, "credited_order_ids": {"$ne": order_id}},
            {
                "$inc": {"total_profit_balance": profit},
                "$set": {"last_updated_profit": profit, "updated_at": now},
                "$addToSet": {"credited_order_ids": order_id},
                "$push": {
                    "history": {
                        "event": "store_profit_backfill",
                        "amount": profit,
                        "order_id": order_id,
                        "created_at": now,
                        "note": "Shared checkout profit credit repair.",
                    }
                },
            },
        )
        if result.modified_count == 1:
            orders.update_one(
                {"_id": order["_id"]},
                {
                    "$set": {
                        "store_profit_credited": True,
                        "store_profit_credited_at": now,
                        "store_profit_credit_source": "shared_checkout_backfill_2026_08",
                    }
                },
            )
            credited_count += 1
            credited_total = round(credited_total + profit, 2)

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode}: {len(candidates)} orders, GHS {sum(x[3] for x in candidates):.2f}")
    for slug in sorted(by_store):
        row = by_store[slug]
        print(f"{slug}: {row['orders']} orders, GHS {row['profit']:.2f}")
    if args.apply:
        print(f"CREDITED: {credited_count} orders, GHS {credited_total:.2f}")


if __name__ == "__main__":
    main()
