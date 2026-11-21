"""Backfill verified store ownership without changing buyers, wallets or statuses.

Defaults to a dry run. Pass --apply to save ownership links and a local audit file.
"""
import argparse
import ast
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from bson import ObjectId
from pymongo import MongoClient, UpdateOne


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    tree = ast.parse(Path(__file__).with_name("db.py").read_text(encoding="utf-8"))
    uri = next(ast.literal_eval(node.value) for node in tree.body
               if isinstance(node, ast.Assign)
               and any(isinstance(target, ast.Name) and target.id == "uri" for target in node.targets))
    client = MongoClient(uri, serverSelectionTimeoutMS=15000)
    db = client["sirhans1"]
    counts = Counter()
    audit = []
    stores = {}
    for store in db.stores.find({}, {"slug": 1, "owner_id": 1}):
        stores.setdefault(store.get("slug"), []).append(store)
    users = {str(user["_id"]) for user in db.users.find({}, {"_id": 1})}
    query = {"store_slug": {"$exists": True, "$nin": [None, ""]}, "store_owner_id": None}
    for order in db.orders.find(query, {"store_slug": 1, "order_id": 1, "store_owner_id": 1}):
        counts["scanned"] += 1
        matches = stores.get(order["store_slug"], [])
        if len(matches) != 1:
            counts["missing_or_ambiguous_store"] += 1
            continue
        owner = matches[0].get("owner_id")
        if isinstance(owner, str) and ObjectId.is_valid(owner):
            owner = ObjectId(owner)
        if not isinstance(owner, ObjectId) or str(owner) not in users:
            counts["invalid_owner"] += 1
            continue
        counts["verified"] += 1
        audit.append({"order_id": order.get("order_id"), "_id": str(order["_id"]),
                      "store_slug": order["store_slug"], "owner_id": str(owner),
                      "previous_field_present": "store_owner_id" in order})

    if args.apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit_path = Path(__file__).with_name(f"store_owner_backfill_{stamp}.json")
        audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
        updates = [UpdateOne(
                {"_id": ObjectId(row["_id"]), "store_slug": row["store_slug"], "store_owner_id": None},
                {"$set": {"store_owner_id": ObjectId(row["owner_id"])}})
            for row in audit]
        if updates:
            counts["updated"] = db.orders.bulk_write(updates, ordered=False).modified_count
        print("Audit file:", audit_path.name)
    print(json.dumps(dict(counts)))
    client.close()


if __name__ == "__main__":
    main()
