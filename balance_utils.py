from datetime import datetime

from db import db


balances_col = db["balances"]


def ensure_user_balance(user_id, now=None):
    """
    Ensure a user has a wallet/balance document.
    Safe to call repeatedly; existing balances are not changed.
    """
    if not user_id:
        return None

    now = now or datetime.utcnow()
    balances_col.update_one(
        {"user_id": user_id},
        {
            "$setOnInsert": {
                "user_id": user_id,
                "amount": 0.00,
                "currency": "GHS",
                "created_at": now,
                "updated_at": now,
            },
        },
        upsert=True,
    )
    return balances_col.find_one({"user_id": user_id})
