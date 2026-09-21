from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from bson import ObjectId

from db import db

announcements_col = db["announcements"]

TARGETS: Dict[str, str] = {
    "customer_dashboard": "Customer Dashboard",
    "index_page": "Index Page",
    "store_page": "Store Page",
}

TONE_STYLES: Dict[str, str] = {
    "info": "Info",
    "success": "Success",
    "warning": "Warning",
    "critical": "Critical",
}

try:
    announcements_col.create_index([("is_active", 1), ("targets", 1), ("created_at", -1)])
except Exception:
    pass


def _fmt_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return ""


def normalize_targets(raw_targets: Any) -> List[str]:
    targets: List[str] = []
    for item in raw_targets or []:
        value = str(item or "").strip()
        if value in TARGETS and value not in targets:
            targets.append(value)
    return targets


def serialize_announcement(doc: Dict[str, Any]) -> Dict[str, Any]:
    tone = str(doc.get("tone") or "info").strip().lower()
    if tone not in TONE_STYLES:
        tone = "info"

    targets = normalize_targets(doc.get("targets"))
    return {
        "_id": str(doc.get("_id") or ""),
        "title": str(doc.get("title") or "").strip(),
        "message": str(doc.get("message") or "").strip(),
        "tone": tone,
        "tone_label": TONE_STYLES[tone],
        "targets": targets,
        "target_labels": [TARGETS[t] for t in targets],
        "is_active": bool(doc.get("is_active", True)),
        "created_at_label": _fmt_dt(doc.get("created_at")),
        "created_by_name": str(doc.get("created_by_name") or "Admin").strip(),
    }


def get_active_announcements(target: str) -> List[Dict[str, Any]]:
    if target not in TARGETS:
        return []

    docs = announcements_col.find(
        {
            "is_active": True,
            "targets": target,
            "deleted_at": {"$exists": False},
        }
    ).sort([("created_at", -1), ("_id", -1)])

    return [serialize_announcement(doc) for doc in docs]


def delete_announcement(announcement_id: str, deleted_by: str) -> bool:
    if not ObjectId.is_valid(announcement_id):
        return False

    result = announcements_col.update_one(
        {"_id": ObjectId(announcement_id), "deleted_at": {"$exists": False}},
        {
            "$set": {
                "deleted_at": datetime.utcnow(),
                "deleted_by": deleted_by,
                "is_active": False,
            }
        },
    )
    return result.modified_count > 0
