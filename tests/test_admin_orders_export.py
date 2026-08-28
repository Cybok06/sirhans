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

    def test_normalize_source_accepts_legacy_non_text_value(self):
        self.assertEqual("main", self.orders._normalize_source({"origin": "unknown"}))


if __name__ == "__main__":
    unittest.main()
