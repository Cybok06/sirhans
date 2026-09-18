"""Regression tests for the admin undelivered-order export helpers."""

import importlib
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock


class _Cursor(list):
    def sort(self, *args, **kwargs):
        return self


def _load_admin_orders():
    # Avoid connecting to the production database while loading the module.
    fake_db = MagicMock()
    sys.modules["db"] = types.SimpleNamespace(db=fake_db)
    sys.modules.pop("admin_orders", None)
    module = importlib.import_module("admin_orders")
    module.services_col.find.return_value = _Cursor([])
    return module


class UndeliveredExportTests(unittest.TestCase):
    def setUp(self):
        self.orders = _load_admin_orders()
        self.orders.orders_col.find.return_value = _Cursor([
            {
                "_id": "order-db-id",
                "order_id": "ORDER-1",
                "created_at": datetime(2026, 8, 28, 9, 0),
                "status": "processing",
                # A malformed legacy source must not stop every export.
                "source": {"origin": "unknown"},
                "items": [
                    {"serviceName": "MTN", "phone": "0200000001", "value": "1GB", "line_status": "delivered"},
                    {"serviceName": "MTN", "phone": "0200000002", "value": "2GB", "line_status": "processing"},
                    # Legacy provider data can hold a non-string status.
                    {"serviceName": "MTN", "phone": "0200000003", "value": "3GB", "line_status": {"state": "queued"}},
                    "malformed legacy line",
                ],
            }
        ])

    def test_collect_export_only_includes_undelivered_lines(self):
        rows, _, _ = self.orders._collect_undelivered_rows({"timeframe": "today"})

        self.assertEqual(["0200000002", "0200000003"], [row["phone"] for row in rows])

    def test_selected_export_only_includes_undelivered_lines(self):
        object_id = "64b64c9f6d3e0a0012345678"
        self.orders.orders_col.find.return_value[0]["_id"] = object_id
        rows, _, _ = self.orders._collect_selected_undelivered_rows({"order_ids": object_id})

        self.assertEqual(["0200000002", "0200000003"], [row["phone"] for row in rows])

    def test_parse_date_accepts_datetime_local_input(self):
        parsed = self.orders._parse_date("2026-08-28T09:30")

        self.assertEqual(datetime(2026, 8, 28, 9, 30), parsed)

    def test_selected_export_includes_failed_lines_from_multiple_orders(self):
        ids = ["64b64c9f6d3e0a0012345678", "64b64c9f6d3e0a0012345679"]
        self.orders.orders_col.find.return_value = _Cursor([
            {
                "_id": order_id,
                "status": "failed",
                "items": [
                    {"phone": f"020000000{index}", "serviceName": "MTN", "value": "2GB", "line_status": "failed"},
                    *[{"phone": "0550000000", "line_status": status} for status in ("delivered", "completed", "refunded", "cancelled")],
                ],
            }
            for index, order_id in enumerate(ids, 1)
        ])

        rows, _, _ = self.orders._collect_selected_undelivered_rows({"order_ids": ",".join(ids)})

        self.assertEqual(["0200000001", "0200000002"], [row["phone"] for row in rows])
        self.assertEqual(ids, [row["order_db_id"] for row in rows])
        self.assertTrue(all(row["status"] == "failed" for row in rows))
        self.orders.orders_col.find.assert_any_call({"_id": {"$in": [self.orders.ObjectId(value) for value in ids]}})

    def test_filter_export_still_excludes_failed_lines(self):
        self.orders.orders_col.find.return_value[0]["items"].append(
            {"phone": "0550000000", "line_status": "failed"}
        )
        rows, _, _ = self.orders._collect_undelivered_rows({"timeframe": "today"})
        self.assertEqual(["0200000002", "0200000003"], [row["phone"] for row in rows])

    def test_normalize_source_accepts_legacy_non_text_value(self):
        self.assertEqual("main", self.orders._normalize_source({"origin": "unknown"}))

    def test_refunded_order_can_be_cancelled_without_another_wallet_credit(self):
        module = self.orders
        module.orders_col = MagicMock()
        module.balances_col = MagicMock()
        module.transactions_col = MagicMock()
        oid = module.ObjectId()
        order = {"_id": oid, "user_id": module.ObjectId(), "status": "failed",
                 "charged_amount": 20, "items": [{"base_amount": 12}]}
        module.orders_col.find_one.return_value = order
        module.orders_col.update_one.return_value.modified_count = 1
        self.assertIn("cancelled", module.ALLOWED_STATUSES)
        self.assertEqual((1, []), module._apply_status_change([oid], "refunded"))
        refund_update = module.orders_col.update_one.call_args.args[1]["$set"]
        self.assertEqual("refunded", refund_update["status"])
        self.assertEqual("refunded", refund_update["items.$[].line_status"])
        order.update(refund_update)
        self.assertEqual((1, []), module._apply_status_change([oid], "cancelled"))
        cancel_update = module.orders_col.update_one.call_args.args[1]["$set"]
        self.assertEqual("cancelled", cancel_update["status"])
        self.assertEqual("cancelled", cancel_update["items.$[].line_status"])
        self.assertNotIn("refunded_at", cancel_update)
        self.assertNotIn("refunded_amount", cancel_update)
        module.balances_col.update_one.assert_called_once()
        module.transactions_col.insert_one.assert_called_once()
        order.update(cancel_update)
        module._apply_status_change([oid], "refunded")
        module.balances_col.update_one.assert_called_once()

    def test_refund_base_uses_base_instead_of_selling_price(self):
        order = {"charged_amount": 25, "total_amount": 30, "items": [{"base_amount": 10}, {"base_amount": 8}]}
        self.assertEqual(18, self.orders._order_refund_base(order))
        order["charged_amount"] = 15
        self.assertEqual(15, self.orders._order_refund_base(order))

    def test_refund_base_legacy_and_zero_charge(self):
        self.assertEqual(12, self.orders._order_refund_base({"charged_amount": 12, "total_amount": 20}))
        self.assertEqual(0, self.orders._order_refund_base({"charged_amount": 0, "items": [{"base_amount": 10}]}))

    def test_refund_base_rejects_incomplete_and_invalid_prices(self):
        for bases in ([10, None], [float("nan")], [-1]):
            with self.assertRaises(ValueError):
                self.orders._order_refund_base({"charged_amount": 20, "items": [{"base_amount": base} for base in bases]})

    def test_wallet_and_ledger_receive_only_base_price_once(self):
        module = self.orders
        module.orders_col = MagicMock()
        module.balances_col = MagicMock()
        module.transactions_col = MagicMock()
        oid = module.ObjectId()
        order = {"_id": oid, "user_id": module.ObjectId(), "status": "failed", "charged_amount": 20,
                 "items": [{"base_amount": 12}], "order_id": "ORDER-REFUND"}
        module.orders_col.find_one.return_value = order
        module.orders_col.update_one.return_value.modified_count = 1
        count, errors = module._apply_status_change([oid], "refunded")
        self.assertEqual((1, []), (count, errors))
        self.assertEqual(12, module.balances_col.update_one.call_args.args[1]["$inc"]["amount"])
        self.assertEqual(12, module.transactions_col.insert_one.call_args.args[0]["amount"])
        saved = module.orders_col.update_one.call_args.args[1]["$set"]
        self.assertEqual(12, saved["refunded_amount"])
        order.update(saved)
        module._apply_status_change([oid], "refunded")
        self.assertEqual(1, module.balances_col.update_one.call_count)

    def test_failed_wallet_credit_does_not_mark_order_refunded(self):
        module = self.orders
        module.orders_col = MagicMock()
        module.balances_col = MagicMock()
        module.transactions_col = MagicMock()
        module.orders_col.find_one.return_value = {
            "user_id": module.ObjectId(), "status": "failed", "charged_amount": 20,
            "items": [{"base_amount": 12}],
        }
        module.balances_col.update_one.side_effect = RuntimeError("Wallet unavailable")
        count, errors = module._apply_status_change([module.ObjectId()], "refunded")
        self.assertEqual(0, count)
        self.assertTrue(errors)
        module.orders_col.update_one.assert_not_called()
        module.transactions_col.insert_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
