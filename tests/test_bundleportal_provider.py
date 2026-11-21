import unittest
from unittest.mock import Mock, patch

import checkout
import order_status
import admin_dashboard


class BundlePortalProviderTests(unittest.TestCase):
    def test_place_order_contract_uses_local_phone_and_idempotency_key(self):
        response = Mock()
        response.status_code = 200
        response.text = '{"success":true}'
        response.json.return_value = {
            "success": True,
            "data": {
                "order_id": "HAN123_0_abc123",
                "reference": "KT-88213",
                "status": "cached",
                "amount": 20.0,
            },
        }

        with patch.object(checkout, "BUNDLEPORTAL_API_KEY", "bp_live_test_key"), patch.object(
            checkout.requests, "post", return_value=response
        ) as post:
            ok, payload = checkout._send_bundleportal_order(
                phone="233241234567",
                network="ishare",
                package_size_gb=5,
                external_ref="HAN123_0_abc123",
                order_id="HAN123",
                debug_events=[],
            )

        self.assertTrue(ok)
        self.assertEqual(payload["data"]["status"], "cached")
        request = post.call_args.kwargs
        self.assertEqual(request["headers"]["x-api-key"], "bp_live_test_key")
        self.assertEqual(
            request["json"],
            {
                "action": "place_order",
                "network": "airteltigo",
                "recipient": "0241234567",
                "package_size": 5,
                "order_id": "HAN123_0_abc123",
            },
        )

    def test_missing_key_fails_without_network_request(self):
        with patch.object(checkout, "BUNDLEPORTAL_API_KEY", ""), patch.object(
            checkout.requests, "post"
        ) as post:
            ok, payload = checkout._send_bundleportal_order(
                phone="0241234567",
                network="mtn",
                package_size_gb=5,
                external_ref="HAN123_0_abc123",
                order_id="HAN123",
                debug_events=[],
            )

        self.assertFalse(ok)
        self.assertEqual(payload["type"], "CONFIG_ERROR")
        post.assert_not_called()


class BundlePortalStatusTests(unittest.TestCase):
    def test_status_mapping(self):
        self.assertEqual(order_status._map_bundleportal_status("processing"), ("processing", "processing"))
        self.assertEqual(order_status._map_bundleportal_status("cached"), ("processing", "cached"))
        self.assertEqual(order_status._map_bundleportal_status("completed"), ("delivered", "success"))
        self.assertEqual(order_status._map_bundleportal_status("failed"), ("failed", "failed"))

    def test_check_status_contract(self):
        response = Mock()
        response.status_code = 200
        response.text = '{"success":true}'
        response.json.return_value = {
            "success": True,
            "data": {
                "order_id": "HAN123_0_abc123",
                "reference": "KT-88213",
                "status": "completed",
                "failure_reason": None,
            },
        }

        with patch.object(order_status, "BUNDLEPORTAL_API_KEY", "bp_live_test_key"), patch.object(
            order_status.requests, "post", return_value=response
        ) as post:
            ok, payload = order_status._fetch_bundleportal_order_status("HAN123_0_abc123", "HAN123")

        self.assertTrue(ok)
        self.assertEqual(payload["data"]["status"], "completed")
        request = post.call_args.kwargs
        self.assertEqual(request["headers"]["x-api-key"], "bp_live_test_key")
        self.assertEqual(
            request["json"],
            {"action": "check_status", "order_reference": "HAN123_0_abc123"},
        )

    def test_completed_status_updates_line_to_delivered(self):
        item = {"provider": "bundleportal", "line_status": "processing", "api_status": "processing"}
        payload = {
            "success": True,
            "data": {
                "order_id": "HAN123_0_abc123",
                "reference": "KT-88213",
                "status": "completed",
                "failure_reason": None,
            },
        }
        now = order_status.datetime.utcnow()
        order_status._apply_bundleportal_status_to_item(item, "completed", payload, now)

        self.assertEqual(item["line_status"], "delivered")
        self.assertEqual(item["api_status"], "success")
        self.assertEqual(item["provider_reference"], "KT-88213")
        self.assertEqual(item["provider_order_id"], "HAN123_0_abc123")


class BundlePortalBalanceTests(unittest.TestCase):
    def setUp(self):
        admin_dashboard._BUNDLEPORTAL_WALLET_CACHE.update(
            {"wallet": None, "currency": "GHS", "ts": None, "raw": None}
        )

    def test_check_balance_contract_and_response(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "success": True,
            "data": {
                "wallet_balance": 180.0,
                "currency": "GHS",
                "user": {"name": "Ama Mensah", "email": "ama@example.com"},
            },
        }

        with patch.object(admin_dashboard, "BUNDLEPORTAL_API_KEY", "bp_live_test_key"), patch.object(
            admin_dashboard.requests, "post", return_value=response
        ) as post:
            result = admin_dashboard.bundleportal_get_wallet_balance(force_refresh=True)

        self.assertTrue(result["ok"])
        self.assertEqual(result["wallet"], 180.0)
        self.assertEqual(result["currency"], "GHS")
        request = post.call_args.kwargs
        self.assertEqual(request["json"], {"action": "check_balance"})
        self.assertEqual(request["headers"]["x-api-key"], "bp_live_test_key")

    def test_cached_balance_avoids_second_api_call(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "success": True,
            "data": {"wallet_balance": 180.0, "currency": "GHS"},
        }

        with patch.object(admin_dashboard, "BUNDLEPORTAL_API_KEY", "bp_live_test_key"), patch.object(
            admin_dashboard.requests, "post", return_value=response
        ) as post:
            first = admin_dashboard.bundleportal_get_wallet_balance(force_refresh=True)
            second = admin_dashboard.bundleportal_get_wallet_balance(force_refresh=False)

        self.assertTrue(first["ok"])
        self.assertTrue(second["cached"])
        self.assertEqual(post.call_count, 1)


if __name__ == "__main__":
    unittest.main()
