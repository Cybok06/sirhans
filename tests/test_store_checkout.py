import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

import importlib
import sys
import types
import mongomock
from mongomock.gridfs import enable_gridfs_integration

enable_gridfs_integration()
with patch.dict(sys.modules, {"db": types.SimpleNamespace(db=mongomock.MongoClient().test)}):
    store_page = importlib.import_module("routes.store_page")


class StoreCheckoutTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(store_page.stores_bp)

    def test_successful_payment_uses_shared_order_processor(self):
        cart = [{"serviceId": "service-1", "phone": "0551234567", "amount": 10}]
        store = {"_id": "store-1", "slug": "test-store"}
        order_result = {
            "success": True,
            "order_id": "HAN123",
            "status": "processing",
            "items": [],
        }

        with patch.object(store_page, "stores_col") as stores, patch.object(
            store_page, "orders_col"
        ) as orders, patch.object(
            store_page, "_server_reprice_store_cart", return_value=(cart, 10.0)
        ), patch.object(
            store_page,
            "_verify_paystack",
            return_value=(True, {"amount": 1020, "currency": "GHS", "channel": "mobile_money"}, ""),
        ), patch.object(
            store_page, "place_store_order", return_value=(order_result.copy(), 200)
        ) as place_order:
            stores.find_one.return_value = store
            orders.find_one.return_value = None
            with self.app.test_client() as client:
                response = client.post(
                    "/store-checkout/test-store",
                    json={
                        "cart": cart,
                        "method": "paystack_inline",
                        "paystack": {"reference": "PAY-REF-1"},
                    },
                )

        self.assertEqual(200, response.status_code)
        body = response.get_json()
        self.assertTrue(body["success"])
        self.assertEqual("HAN123", body["order_id"])
        self.assertEqual(10.2, body["paid_ghs"])
        payload = place_order.call_args.args[0]
        self.assertEqual("paid", payload["payment_status"])
        self.assertEqual("PAY-REF-1", payload["paystack_reference"])
        self.assertEqual(10.0, payload["charged_amount"])
        self.assertEqual(0.2, payload["gateway_fee_overage_ghs"])

    def test_verified_payment_preserves_all_paid_bulk_items(self):
        paid_cart = [{"serviceId": "service-1", "phone": phone, "amount": 10}
                     for phone in ("0551234567", "0551234568", "0551234569")]
        def reprice(store, cart):
            return cart, sum(item["amount"] for item in cart)
        with patch.object(store_page, "stores_col") as stores, patch.object(
            store_page, "orders_col"
        ) as orders, patch.object(store_page, "_server_reprice_store_cart", side_effect=reprice), patch.object(
            store_page, "_verify_paystack", return_value=(True, {
                "amount": 3060, "currency": "GHS",
                "metadata": {"slug": "test-store", "checkout_cart": paid_cart},
            }, "")
        ), patch.object(store_page, "place_store_order", return_value=({"success": True}, 200)) as place:
            stores.find_one.return_value = {"slug": "test-store"}
            orders.find_one.return_value = None
            response = self.app.test_client().post("/store-checkout/test-store", json={
                "cart": paid_cart[:1], "paystack": {"reference": "BULK-REF"},
            })
        self.assertEqual(200, response.status_code)
        self.assertEqual(paid_cart, place.call_args.args[0]["cart"])
        self.assertEqual(30, place.call_args.args[0]["charged_amount"])

    def test_bulk_split_keeps_each_pending_job_with_its_own_order(self):
        results = [{"amount": 10, "provider_request_order_id": f"JOB-{i}"} for i in range(3)]
        jobs = [{"provider_request_order_id": f"JOB-{i}"} for i in range(3)]
        with patch.object(store_page, "_save_store_order_batch") as save, patch.dict(
            store_page._checkout_helpers, {"order_fn": None}
        ), patch.object(store_page, "generate_order_id", side_effect=["LINE-0", "LINE-1", "LINE-2"]):
            created, mapped = store_page._persist_store_split_orders(
                base_order_fields={"order_id": "GROUP", "pending_provider_jobs": jobs},
                results=results, api_jobs=jobs,
            )
        self.assertEqual(3, len(save.call_args.args[0]))
        self.assertEqual(30, sum(doc["charged_amount"] for doc in created))
        for i, doc in enumerate(created):
            self.assertEqual([jobs[i]], doc["pending_provider_jobs"])
            self.assertEqual((f"LINE-{i}", [jobs[i]]), mapped[i])

    def test_bulk_receipt_contains_every_recipient_and_full_total(self):
        with patch.dict(sys.modules, {"db": types.SimpleNamespace(db=mongomock.MongoClient().test)}):
            checkout = importlib.import_module("checkout")
        orders = mongomock.MongoClient().test.orders
        for i in range(3):
            orders.insert_one({
                "order_id": f"LINE-{i}", "store_slug": "test-store",
                "items": [{"phone": f"055123456{i}", "amount": 10}],
                "total_amount": 10, "charged_amount": 10, "status": "pending",
                "debug": {"bulk_group_order_id": "GROUP", "bulk_group_index": i + 1},
            })
        with self.app.test_request_context(), patch.object(checkout, "orders_col", orders), patch.object(
            checkout, "render_template", return_value="receipt"
        ) as render:
            checkout.invoice_view("LINE-0")
        receipt = render.call_args.kwargs["order"]
        self.assertEqual(3, len(receipt["items"]))
        self.assertEqual(30, receipt["charged_amount"])
        self.assertEqual(1, len(orders.find_one({"order_id": "LINE-0"})["items"]))

    def test_split_orders_are_inserted_once_without_recursion(self):
        results = [
            {
                "amount": 10.0,
                "profit_amount": 1.0,
                "line_status": "processing",
                "provider_request_order_id": "JOB-1",
            }
        ]
        jobs = [{"provider_request_order_id": "JOB-1", "provider": "bundleportal"}]
        orders = MagicMock()

        with patch.object(store_page, "_save_store_order_batch") as save, patch.dict(
            store_page._checkout_helpers, {"order_fn": None}, clear=False
        ), patch.object(store_page, "generate_order_id", side_effect=["HAN-LINE-1"]):
            created, order_jobs = store_page._persist_store_split_orders(
                base_order_fields={"order_id": "HAN-GROUP", "status": "pending"},
                results=results,
                api_jobs=jobs,
            )

        save.assert_called_once()
        self.assertEqual("HAN-LINE-1", created[0]["order_id"])
        self.assertEqual("HAN-GROUP", created[0]["debug"]["bulk_group_order_id"])
        self.assertEqual([("HAN-LINE-1", jobs)], order_jobs)

    def test_batch_save_uses_one_transaction_without_individual_fallback(self):
        database = MagicMock()
        active_session = database.client.start_session.return_value.__enter__.return_value
        active_session.with_transaction.side_effect = lambda callback: callback(active_session)
        with patch.object(store_page, "db", database), patch.object(store_page, "orders_col") as orders:
            orders.insert_many.side_effect = RuntimeError("second line failed")
            with self.assertRaises(RuntimeError):
                store_page._save_store_order_batch([{"order_id": "A"}, {"order_id": "B"}])
            orders.insert_many.assert_called_once_with(
                [{"order_id": "A"}, {"order_id": "B"}], session=active_session)
            orders.insert_one.assert_not_called()

    def test_provider_failure_does_not_stop_remaining_paid_orders(self):
        jobs = [("A", [{"job": 1}]), ("B", [{"job": 2}])]
        with patch.object(store_page, "orders_col") as orders, patch.object(
            store_page, "_background_process_providers", side_effect=[RuntimeError("timeout"), None]
        ) as process:
            store_page._process_store_order_jobs(jobs)
        self.assertEqual(2, process.call_count)
        self.assertTrue(orders.update_one.call_args.args[1]["$set"]["provider_processing_needs_review"])

    def test_payment_retry_returns_every_line_and_full_charge(self):
        orders = mongomock.MongoClient().test.orders
        for i in range(2):
            orders.insert_one({"order_id": f"LINE-{i}", "store_slug": "test-store",
                "paystack_reference": "REF", "charged_amount": 10, "items": [{"phone": str(i)}],
                "debug": {"bulk_group_index": i + 1, "bulk_group_size": 2}})
        with patch.object(store_page, "orders_col", orders), patch.object(store_page, "stores_col") as stores:
            stores.find_one.return_value = {"slug": "test-store"}
            response = self.app.test_client().post("/store-checkout/test-store", json={
                "cart": [{"amount": 10}], "paystack": {"reference": "REF"}})
        self.assertEqual(200, response.status_code)
        self.assertEqual(20, response.json["charged_amount"])
        self.assertEqual(2, len(response.json["items"]))

    def test_partial_legacy_batch_is_not_reported_as_success(self):
        orders = mongomock.MongoClient().test.orders
        orders.insert_one({"order_id": "ONLY-LINE", "store_slug": "test-store",
            "paystack_reference": "REF", "debug": {"bulk_group_size": 2}})
        with patch.object(store_page, "orders_col", orders), patch.object(store_page, "stores_col") as stores:
            stores.find_one.return_value = {"slug": "test-store"}
            response = self.app.test_client().post("/store-checkout/test-store", json={
                "cart": [{"amount": 10}], "paystack": {"reference": "REF"}})
        self.assertEqual(409, response.status_code)
        self.assertFalse(response.json["success"])

    def test_same_payment_reuses_line_database_identities(self):
        batches = []
        with patch.object(store_page, "_save_store_order_batch", side_effect=lambda docs: batches.append(docs)):
            for _ in range(2):
                store_page._persist_store_split_orders(
                    base_order_fields={"store_slug": "test", "paystack_reference": "REF"},
                    results=[{"amount": 10}, {"amount": 10}], api_jobs=[])
        self.assertEqual([d["_id"] for d in batches[0]], [d["_id"] for d in batches[1]])
        self.assertNotEqual(batches[0][0]["_id"], batches[0][1]["_id"])


if __name__ == "__main__":
    unittest.main()
