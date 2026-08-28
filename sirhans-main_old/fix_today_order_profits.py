import argparse
import ast
import json
from copy import deepcopy
from datetime import datetime, timedelta

from bson import ObjectId

from db import db


orders_col = db["orders"]
services_col = db["services"]


def _to_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _money(value):
    return round(float(_to_float(value, 0.0) or 0.0), 2)


def _coerce_value_obj(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                try:
                    parsed = ast.literal_eval(text)
                    return parsed if isinstance(parsed, dict) else {}
                except Exception:
                    return {}
    return {}


def _normalize_offer_value(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else value
            except Exception:
                try:
                    parsed = ast.literal_eval(text)
                    return parsed if isinstance(parsed, dict) else value
                except Exception:
                    return value
    return value


def _pick_normal_base_amount(service_doc, value_obj, raw_value):
    offers = service_doc.get("offers") or []
    wanted_id = (value_obj or {}).get("id")
    wanted_volume = (value_obj or {}).get("volume")

    for offer in offers:
        offer_value = _normalize_offer_value(offer.get("value"))
        offer_amount = _to_float(offer.get("amount"))
        if offer_amount is None:
            continue

        if isinstance(offer_value, dict):
            if wanted_id is not None and offer_value.get("id") == wanted_id:
                return round(float(offer_amount), 2)
            if wanted_volume is not None and offer_value.get("volume") == wanted_volume:
                return round(float(offer_amount), 2)
        elif raw_value is not None and offer_value == raw_value:
            return round(float(offer_amount), 2)

    return None


def _extract_provider_cost(item):
    direct = _to_float(item.get("provider_package_amount"))
    if direct is not None and direct > 0:
        return round(float(direct), 2)

    response = item.get("api_response")
    if not isinstance(response, dict):
        return None

    data_block = response.get("data") if isinstance(response.get("data"), dict) else {}
    for src in (response, data_block):
        for key in ("price", "cost", "package_amount", "provider_amount"):
            val = _to_float(src.get(key))
            if val is not None and val > 0:
                return round(float(val), 2)
    return None


def _service_for_item(item, cache):
    service_id = item.get("serviceId") or item.get("service_id")
    if not service_id:
        return None

    key = str(service_id)
    if key in cache:
        return cache[key]

    try:
        service = services_col.find_one(
            {"_id": ObjectId(key)},
            {"offers": 1, "name": 1},
        )
    except Exception:
        service = None

    cache[key] = service
    return service


def _repair_order(order, service_cache):
    items = order.get("items") or []
    repaired_items = []
    total_profit = 0.0
    changed = False
    priced_lines = 0
    unpriced_lines = 0

    for original_item in items:
        item = deepcopy(original_item)
        amount = _money(item.get("amount"))
        line_status = str(item.get("line_status") or "").strip().lower()

        if amount <= 0 or line_status.startswith("skipped"):
            new_profit = 0.0
            if _money(item.get("profit_amount")) != new_profit:
                item["profit_amount"] = new_profit
                item["profit_percent_used"] = 0.0
                changed = True
            repaired_items.append(item)
            continue

        base_amount = _extract_provider_cost(item)

        if base_amount is None or base_amount <= 0:
            unpriced_lines += 1
            total_profit += _money(item.get("profit_amount"))
            repaired_items.append(item)
            continue

        priced_lines += 1
        new_profit = max(0.0, round(amount - base_amount, 2))
        new_percent = round((new_profit / base_amount) * 100.0, 2) if base_amount > 0 else 0.0

        if (
            _money(item.get("base_amount")) != base_amount
            or _money(item.get("profit_amount")) != new_profit
            or _money(item.get("profit_percent_used")) != new_percent
        ):
            item["base_amount"] = base_amount
            item["profit_amount"] = new_profit
            item["profit_percent_used"] = new_percent
            changed = True

        total_profit += new_profit
        repaired_items.append(item)

    total_profit = round(total_profit, 2)
    if _money(order.get("profit_amount_total")) != total_profit:
        changed = True

    return {
        "changed": changed,
        "items": repaired_items,
        "profit_amount_total": total_profit,
        "priced_lines": priced_lines,
        "unpriced_lines": unpriced_lines,
    }


def _day_window(date_text):
    if date_text:
        start = datetime.strptime(date_text, "%Y-%m-%d")
    else:
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def main():
    parser = argparse.ArgumentParser(
        description="Recalculate today's agent checkout order profits from provider cost prices."
    )
    parser.add_argument("--date", help="UTC/Ghana date to repair, format YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without updating MongoDB.")
    args = parser.parse_args()

    start, end = _day_window(args.date)
    query = {
        "created_at": {"$gte": start, "$lt": end},
        "store_slug": {"$exists": False},
        "paid_from": {"$ne": "public_paystack"},
    }

    service_cache = {}
    scanned = changed = priced_lines = unpriced_lines = 0
    before_total = after_total = 0.0

    for order in orders_col.find(query).sort("created_at", 1):
        scanned += 1
        before_total += _money(order.get("profit_amount_total"))
        repair = _repair_order(order, service_cache)
        after_total += repair["profit_amount_total"]
        priced_lines += repair["priced_lines"]
        unpriced_lines += repair["unpriced_lines"]

        if not repair["changed"]:
            continue

        changed += 1
        print(
            f"{order.get('order_id') or order.get('_id')}: "
            f"{_money(order.get('profit_amount_total')):.2f} -> {repair['profit_amount_total']:.2f}"
        )

        if not args.dry_run:
            orders_col.update_one(
                {"_id": order["_id"]},
                {
                    "$set": {
                        "items": repair["items"],
                        "profit_amount_total": repair["profit_amount_total"],
                        "profit_repaired_at": datetime.utcnow(),
                        "profit_repair_note": "Recalculated from agent price minus provider cost price.",
                        "updated_at": datetime.utcnow(),
                    }
                },
            )

    mode = "DRY RUN" if args.dry_run else "UPDATED"
    print("")
    print(f"{mode}: {start.date()} orders scanned: {scanned}")
    print(f"Orders changed: {changed}")
    print(f"Priced lines: {priced_lines}")
    print(f"Lines without matching normal offer: {unpriced_lines}")
    print(f"Profit total before: GHS {round(before_total, 2):.2f}")
    print(f"Profit total after:  GHS {round(after_total, 2):.2f}")


if __name__ == "__main__":
    main()
