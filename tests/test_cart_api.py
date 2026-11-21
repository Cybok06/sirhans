import unittest

import cart_api


class CartNormalizationTests(unittest.TestCase):
    def test_offer_display_label_survives_server_cart_sync(self):
        item, error = cart_api._normalize_item(
            {
                "serviceId": "service-1",
                "serviceName": "MTN NORMAL",
                "phone": "0530393625",
                "value": {"id": 1, "volume": 1000},
                "value_obj": {"id": 1, "volume": 1000},
                "value_text": "1GB",
                "amount": 3.95,
            }
        )

        self.assertIsNone(error)
        self.assertEqual("1GB", item["value_text"])


if __name__ == "__main__":
    unittest.main()
