from __future__ import annotations

from collections import Counter

from db import db
from paystack_webhook import (
    _is_terminal_verify_failure,
    complete_arkesel_ussd_payment_by_reference,
    mark_arkesel_payment_failed,
    verify_paystack_transaction,
)


def main() -> None:
    pending_query = {
        "channel": "arkesel_ussd",
        "payment_reference": {"$exists": True, "$ne": ""},
        "status": {"$in": ["charge_initiating", "awaiting_payment", "payment_processing", "creating_order"]},
    }
    existing_order_query = {
        "channel": "arkesel_ussd",
        "paystack_reference": {"$exists": True, "$ne": ""},
        "payment_status": {"$ne": "paid"},
    }

    scanned = 0
    repaired = 0
    still_pending = 0
    marked_failed = 0
    failed = 0
    reasons = Counter()

    refs_seen: set[str] = set()

    for doc in db["ussd_pending_payments"].find(pending_query, {"payment_reference": 1, "paystack_reference": 1}):
        ref = str(doc.get("payment_reference") or doc.get("paystack_reference") or "").strip()
        if ref:
            refs_seen.add(ref)

    for doc in db["orders"].find(existing_order_query, {"payment_reference": 1, "paystack_reference": 1}):
        ref = str(doc.get("payment_reference") or doc.get("paystack_reference") or "").strip()
        if ref:
            refs_seen.add(ref)

    for reference in sorted(refs_seen):
        scanned += 1
        ok, payload, reason = verify_paystack_transaction(reference)
        if not ok:
            if _is_terminal_verify_failure(payload if isinstance(payload, dict) else {}, reason):
                mark_arkesel_payment_failed(reference, reason, payload if isinstance(payload, dict) else {})
                marked_failed += 1
                reasons[reason or "marked_failed"] += 1
                continue
            still_pending += 1
            reasons[reason or "pending"] += 1
            continue

        result = complete_arkesel_ussd_payment_by_reference(reference, payload)
        if result.get("success"):
            repaired += 1
        else:
            failed += 1
            reasons[str(result.get("message") or "completion_failed")] += 1

    print(
        {
            "scanned_references": scanned,
            "repaired": repaired,
            "still_pending": still_pending,
            "marked_failed": marked_failed,
            "failed": failed,
            "reasons": dict(reasons),
        }
    )


if __name__ == "__main__":
    main()
