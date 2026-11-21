from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from db import db

users_col = db["users"]
agent_codes_col = db["agent_codes"]


def ensure_agent_code_indexes() -> None:
    agent_codes_col.create_index("user_id", unique=True)
    agent_codes_col.create_index("agent_code", unique=True)


def _generate_four_digit_code(existing_codes: set[str]) -> str:
    if len(existing_codes) >= 9000:
        raise RuntimeError("All four digit agent codes are already in use.")

    while True:
        code = f"{random.randint(1000, 9999)}"
        if code not in existing_codes:
            return code


def generate_missing_agent_codes() -> dict[str, int]:
    ensure_agent_code_indexes()
    now = datetime.utcnow()

    existing_codes = {
        str(doc.get("agent_code"))
        for doc in agent_codes_col.find({}, {"agent_code": 1})
        if doc.get("agent_code")
    }
    existing_user_ids = {
        doc.get("user_id")
        for doc in agent_codes_col.find({}, {"user_id": 1})
        if doc.get("user_id")
    }

    query = {
        "role": "customer",
        "$or": [{"deleted": {"$exists": False}}, {"deleted": False}],
    }

    created = 0
    skipped = 0
    for user in users_col.find(query, {"_id": 1}):
        user_id = user["_id"]
        if user_id in existing_user_ids:
            skipped += 1
            continue

        code = _generate_four_digit_code(existing_codes)
        agent_codes_col.insert_one(
            {
                "user_id": user_id,
                "agent_code": code,
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
        )
        existing_codes.add(code)
        existing_user_ids.add(user_id)
        created += 1

    return {"created": created, "skipped": skipped}


def create_agent_code_for_user(user_id: ObjectId) -> str:
    ensure_agent_code_indexes()
    now = datetime.utcnow()

    existing_doc = agent_codes_col.find_one({"user_id": user_id}, {"agent_code": 1})
    if existing_doc and existing_doc.get("agent_code"):
        return str(existing_doc["agent_code"])

    for _ in range(20):
        existing_codes = {
            str(doc.get("agent_code"))
            for doc in agent_codes_col.find({}, {"agent_code": 1})
            if doc.get("agent_code")
        }
        code = _generate_four_digit_code(existing_codes)

        try:
            agent_codes_col.insert_one(
                {
                    "user_id": user_id,
                    "agent_code": code,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }
            )
            return code
        except DuplicateKeyError:
            existing_doc = agent_codes_col.find_one({"user_id": user_id}, {"agent_code": 1})
            if existing_doc and existing_doc.get("agent_code"):
                return str(existing_doc["agent_code"])

    raise RuntimeError("Could not create a unique agent code for this user.")


def regenerate_agent_code(user_id: ObjectId, admin_id: Any | None = None) -> str:
    ensure_agent_code_indexes()
    now = datetime.utcnow()
    existing_codes = {
        str(doc.get("agent_code"))
        for doc in agent_codes_col.find({"user_id": {"$ne": user_id}}, {"agent_code": 1})
        if doc.get("agent_code")
    }
    code = _generate_four_digit_code(existing_codes)

    agent_codes_col.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "agent_code": code,
                "status": "active",
                "updated_at": now,
                "updated_by": admin_id,
            },
            "$setOnInsert": {
                "user_id": user_id,
                "created_at": now,
            },
        },
        upsert=True,
    )
    return code
