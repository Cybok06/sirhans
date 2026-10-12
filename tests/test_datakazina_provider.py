import unittest
from unittest.mock import Mock, patch

import checkout
from routes import store_page
from bson import ObjectId


PACKAGES = [
    {"id": 1, "network_id": 3, "volume": "1", "status": "In Stock"},
    {"id": 11, "network_id": 3, "volume": "12", "status": "In Stock"},
    {"id": 50, "network_id": 6, "volume": "1", "status": "In Stock"},
    {"id": 59, "network_id": 6, "volume": "15", "status": "In Stock"},
    {"id": 64, "network_id": 6, "volume": "50", "status": "In Stock"},
]


class DataKazinaTests(unittest.TestCase):
    def test_route_choice_overrides_both_mtn_service_names(self):
        from datakazina_routes import network_id
        for service in ("MTN NORMAL", "MTN EXPRESS"):
            self.assertEqual(3, network_id("datakazina_mtn", service))
            self.assertEqual(6, network_id("datakazina_mtn_express", service))
        self.assertEqual(3, network_id("datakazina", "MTN NORMAL"))
        self.assertEqual(6, network_id("datakazina", "MTN EXPRESS"))

    def test_services_accept_both_routes_for_both_mtn_services(self):
        import admin_services
        import mongomock
        from flask import Flask
        app = Flask(__name__)
        services = mongomock.MongoClient().test.services
        for name in ("MTN NORMAL", "MTN EXPRESS", "TELECEL"):
            service_id = services.insert_one({"name": name}).inserted_id
            for provider in ("datakazina_mtn", "datakazina_mtn_express"):
                with app.test_request_context(json={"provider": provider}), patch.object(
                    admin_services, "_require_admin", return_value=True
                ), patch.object(admin_services, "can_manage_providers", return_value=True), patch.object(
                    admin_services, "services_col", services
                ):
                    response = app.make_response(admin_services.set_service_provider(str(service_id)))
                    status = response.status_code
                self.assertEqual(400 if name == "TELECEL" else 200, status)
                if status == 200:
                    self.assertEqual(provider, services.find_one({"_id": service_id})["provider"])

    def test_route_choices_have_short_managed_admin_labels(self):
        import admin_access
        from flask import Flask, g
        with Flask(__name__).test_request_context():
            g.admin_user = {"role": "admin", "managed_admin": True}
            self.assertEqual("DTK", admin_access.provider_display_name("datakazina_mtn"))
            self.assertEqual("DTK-E", admin_access.provider_display_name("datakazina_mtn_express"))
            self.assertEqual("DTK-E unavailable", admin_access.provider_status_text("DataKazina MTN Express unavailable"))

    def test_size_maps_to_package_id_on_selected_network(self):
        with patch.object(checkout, "_datakazina_get_packages_cached", return_value=PACKAGES):
            for network, size, expected in [(3, 1, 1), (3, 12, 11), (6, 1, 50), (6, 15, 59), (6, 50, 64)]:
                with self.subTest(network=network, size=size):
                    self.assertEqual(expected, checkout._resolve_datakazina_shared_bundle(
                        {"id": size, "volume": size * 1000}, {"value": f"{size}GB"}, network))
            self.assertIsNone(checkout._resolve_datakazina_shared_bundle({}, {"value": "12GB"}, 6))
            self.assertEqual(50, checkout._resolve_datakazina_shared_bundle({"id": 50}, {}, 6))
            self.assertIsNone(checkout._resolve_datakazina_shared_bundle({"id": 1}, {}, 6))

    def test_post_uses_documented_fields_and_express_network(self):
        response = Mock(status_code=200, text="", json=Mock(return_value={"success": True, "transaction_code": "TX-1"}))
        with patch.object(checkout.requests, "post", return_value=response) as post, patch.object(checkout, "DATAKAZINA_API_KEY", "test-key"):
            result = checkout._datakazina_submit_single("0551234567", 50, "UNIQUE-1", network_id=6)
        self.assertTrue(result["ok"])
        self.assertEqual("TX-1", result["provider_reference"])
        self.assertTrue(post.call_args.args[0].endswith("/api/v1/buy-data-package"))
        self.assertEqual("test-key", post.call_args.kwargs["headers"]["x-api-key"])
        self.assertEqual({"recipient_msisdn": "0551234567", "network_id": 6,
                          "shared_bundle": 50, "incoming_api_ref": "UNIQUE-1"}, post.call_args.kwargs["json"])

    def test_rejected_response_is_not_marked_successful(self):
        response = Mock(status_code=200, text="", json=Mock(return_value={"success": "false"}))
        with patch.object(checkout.requests, "post", return_value=response):
            self.assertFalse(checkout._datakazina_submit_single("0551234567", 50, "REF", network_id=6)["ok"])

    def test_bulk_worker_posts_each_recipient_and_saves_provider_reference(self):
        import mongomock
        orders = mongomock.MongoClient().test.orders
        jobs = []
        for i in range(3):
            ref = f"REF-{i}"
            orders.insert_one({"order_id": f"ORDER-{i}", "items": [{"provider_request_order_id": ref, "amount": 5}]})
            jobs.append({"phone": f"055123456{i}", "provider": ("datakazina", "datakazina_mtn_express", "datakazina_mtn_express")[i], "network_id": 6,
                         "shared_bundle": 50, "provider_request_order_id": ref})
        response = Mock(status_code=200, text="", json=Mock(return_value={"success": True, "transaction_code": "TX"}))
        with patch.object(checkout, "orders_col", orders), patch.object(checkout.requests, "post", return_value=response) as post:
            for i, job in enumerate(jobs):
                checkout._background_process_providers(f"ORDER-{i}", [job])
        self.assertEqual(3, post.call_count)
        for i, call in enumerate(post.call_args_list):
            self.assertEqual(6, call.kwargs["json"]["network_id"])
            self.assertEqual(f"REF-{i}", call.kwargs["json"]["incoming_api_ref"])
            item = orders.find_one({"order_id": f"ORDER-{i}"})["items"][0]
            self.assertEqual("success", item["api_status"])
            self.assertEqual("TX", item["provider_reference"])

    def test_both_routes_can_be_selected_on_both_mtn_services(self):
        for service_name in ("MTN NORMAL", "MTN EXPRESS"):
            for provider, expected_network, expected_package in (("datakazina_mtn", 3, 1), ("datakazina_mtn_express", 6, 50)):
                with self.subTest(service=service_name, provider=provider):
                    self._check_bulk_route(service_name, provider, expected_network, expected_package)

    def test_express_bulk_store_creates_and_posts_every_line(self):
        self._check_bulk_route("MTN EXPRESS", "datakazina", 6, 50)

    def _check_bulk_route(self, service_name, provider, expected_network, expected_package):
        service_id = ObjectId()
        service = {"_id": service_id, "name": service_name, "type": "API", "provider": provider}
        cart = [{"serviceId": str(service_id), "phone": f"055123456{i}", "amount": 5,
                 "value": "1GB", "value_obj": {"volume": 1000}} for i in range(3)]
        with patch.object(store_page, "_server_reprice_store_cart", return_value=(cart, 15)), patch.object(
            store_page, "services_col"
        ) as services, patch.object(store_page, "_service_unavailability_reason", return_value=(False, "")), patch.object(
            checkout, "_datakazina_get_packages_cached", return_value=PACKAGES
        ), patch.object(store_page, "orders_col") as orders, patch.object(store_page, "transactions_col"), patch.object(
            store_page, "_credit_paid_store_order_profits"
        ), patch.dict(store_page._checkout_helpers, {"order_fn": None}), patch.object(
            store_page, "_background_process_providers"
        ) as process:
            services.find_one.return_value = service
            orders.find_one.return_value = None
            result, status = store_page.place_store_order({"store_doc": {"slug": "test"}, "cart": cart, "payment_status": "paid"})
        self.assertEqual(200, status)
        self.assertEqual(3, len(result["order_ids"]))
        self.assertEqual(3, orders.insert_one.call_count)
        self.assertEqual(3, process.call_count)
        refs = []
        for call in process.call_args_list:
            job = call.args[1][0]
            self.assertEqual(expected_network, job["network_id"])
            self.assertEqual(expected_package, job["shared_bundle"])
            self.assertEqual(provider, job["provider"])
            refs.append(job["provider_request_order_id"])
        self.assertEqual(3, len(set(refs)))


if __name__ == "__main__":
    unittest.main()
