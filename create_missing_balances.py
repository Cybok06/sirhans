import argparse
from datetime import datetime

from balance_utils import ensure_user_balance
from db import db


users_col = db["users"]
balances_col = db["balances"]


def _customer_query(include_deleted=False):
    query = {"role": "customer"}
    if not include_deleted:
        query["$or"] = [{"deleted": {"$exists": False}}, {"deleted": False}]
    return query


def main():
    parser = argparse.ArgumentParser(
        description="Create missing wallet/balance documents for customers and agents."
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview missing balances without creating them.")
    parser.add_argument("--include-deleted", action="store_true", help="Also include soft-deleted customer accounts.")
    args = parser.parse_args()

    query = _customer_query(include_deleted=args.include_deleted)
    now = datetime.utcnow()

    scanned = 0
    missing = 0
    created = 0

    projection = {
        "_id": 1,
        "first_name": 1,
        "last_name": 1,
        "username": 1,
        "phone": 1,
        "agent_level": 1,
        "status": 1,
    }

    for user in users_col.find(query, projection).sort("_id", 1):
        scanned += 1
        user_id = user["_id"]
        if balances_col.find_one({"user_id": user_id}, {"_id": 1}):
            continue

        missing += 1
        name = (
            f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
            or user.get("username")
            or str(user_id)
        )
        level = user.get("agent_level") or "normal"
        print(f"Missing balance: {name} | {user.get('phone', '')} | level={level} | id={user_id}")

        if not args.dry_run:
            ensure_user_balance(user_id, now)
            created += 1

    mode = "DRY RUN" if args.dry_run else "UPDATED"
    print("")
    print(f"{mode}: customers scanned: {scanned}")
    print(f"Missing balances found: {missing}")
    print(f"Balance documents created: {created}")


if __name__ == "__main__":
    main()
