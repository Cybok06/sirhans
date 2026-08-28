import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from routes import store_page


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

        with patch.object(store_page, "orders_col", orders), patch.dict(
            store_page._checkout_helpers, {"order_fn": None}, clear=False
        ), patch.object(store_page, "generate_order_id", side_effect=["HAN-LINE-1"]):
            created, order_jobs = store_page._persist_store_split_orders(
                base_order_fields={"order_id": "HAN-GROUP", "status": "pending"},
                results=results,
                api_jobs=jobs,
            )

        orders.insert_one.assert_called_once()
        self.assertEqual("HAN-LINE-1", created[0]["order_id"])
        self.assertEqual("HAN-GROUP", created[0]["debug"]["bulk_group_order_id"])
        self.assertEqual([("HAN-LINE-1", jobs)], order_jobs)


if __name__ == "__main__":
    unittest.main()
