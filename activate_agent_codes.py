from datetime import datetime

from db import db

agent_codes_col = db["agent_codes"]


if __name__ == "__main__":
    now = datetime.utcnow()
    result = agent_codes_col.update_many(
        {
            "$or": [
                {"status": {"$exists": False}},
                {"status": ""},
                {"status": None},
            ]
        },
        {"$set": {"status": "active", "updated_at": now}},
    )
    print(
        "Agent code status update complete: "
        f"{result.modified_count} documents set to active."
    )
