from datetime import datetime
import random
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from db import db

agent_codes_col = db["agent_codes"]


def generate_unique_code() -> str:
    existing_codes = {
        str(doc.get("agent_code"))
        for doc in agent_codes_col.find({}, {"agent_code": 1})
        if doc.get("agent_code")
    }

    if len(existing_codes) >= 9000:
        raise RuntimeError("All four digit agent codes are already in use.")

    while True:
        code = f"{random.randint(1000, 9999)}"
        if code not in existing_codes:
            return code


if __name__ == "__main__":
    existing_public = agent_codes_col.find_one({"type": "public"})
    if existing_public:
        print(
            "Public agent code already exists: "
            f"{existing_public.get('agent_code')}"
        )
    else:
        now = datetime.utcnow()
        code = generate_unique_code()
        result = agent_codes_col.insert_one(
            {
                "agent_code": code,
                "created_at": now,
                "updated_at": now,
                "status": "active",
                "type": "public",
            }
        )
        print(f"Inserted public agent code {code} with id {result.inserted_id}.")
