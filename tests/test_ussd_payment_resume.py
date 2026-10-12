import importlib
import sys
import types
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask


class FakeDB:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, MagicMock(name=name))


def load_ussd_module():
    fake_db = FakeDB()
    db_module = types.ModuleType("db")
    db_module.db = fake_db

    deposit_module = types.ModuleType("deposit")
    deposit_module.PAYSTACK_SECRET_KEY = "test-secret"

    store_module = types.ModuleType("routes.store_page")
    store_module._apply_store_pricing_to_service = MagicMock()
    store_module._build_pricing_map = MagicMock()
    store_module._extract_store_whatsapp = MagicMock()
    store_module._load_services_for_store_view = MagicMock()
    store_module.generate_order_id = MagicMock(return_value="ORDER-1")
    store_module.place_store_order = MagicMock()
    store_module.stores_col = MagicMock()

    sys.modules.pop("routes.arkesel_ussd", None)
    with patch.dict(
        sys.modules,
        {"db": db_module, "deposit": deposit_module, "routes.store_page": store_module},
    ):
        module = importlib.import_module("routes.arkesel_ussd")
    return module, fake_db


def load_paystack_module():
    fake_db = FakeDB()
    db_module = types.ModuleType("db")
    db_module.db = fake_db

    deposit_module = types.ModuleType("deposit")
    deposit_module.PAYSTACK_SECRET_KEY = "test-secret"

    checkout_module = types.ModuleType("checkout")
    checkout_module._background_process_providers = MagicMock()
    checkout_module.jlog = MagicMock()

    sys.modules.pop("paystack_webhook", None)
    with patch.dict(
        sys.modules,
        {"db": db_module, "deposit": deposit_module, "checkout": checkout_module},
    ):
        module = importlib.import_module("paystack_webhook")
    return module, fake_db


class UssdPaymentResumeTests(unittest.TestCase):
    def setUp(self):
        self.ussd, self.db = load_ussd_module()
        self.pending = self.db["ussd_pending_payments"]
        self.sessions = self.db["ussd_sessions"]
        self.recent_agents = self.db["ussd_recent_agents"]
        self.pending.update_one.return_value = SimpleNamespace(modified_count=1)
        self.sessions.update_one.return_value = SimpleNamespace(modified_count=1)

    def test_new_session_resumes_otp_without_agent_code(self):
        self.pending.find_one.return_value = {
            "_id": "pending-1",
            "status": "awaiting_paystack_otp",
            "payment_status": "pending",
            "payer_phone": "0551234567",
            "order_id": "ORDER-1",
            "payment_reference": "REF-1",
            "otp_display_text": "Enter the voucher sent to your phone",
            "otp_expires_at": datetime.utcnow() + timedelta(minutes=5),
        }
        app = Flask(__name__)

        with app.app_context():
            response = self.ussd._handle_arkesel_json(
                {
                    "sessionID": "new-session",
                    "userID": "user-1",
                    "msisdn": "233551234567",
                    "serviceCode": "*123#",
                    "network": "MTN",
                    "newSession": True,
                }
            )
            body = response.get_json()

        self.assertTrue(body["continueSession"])
        self.assertIn("without entering your agent code", body["message"])
        self.assertNotIn("Enter Agent Code", body["message"])

    def test_invalid_otp_remains_retryable(self):
        self.sessions.find_one.return_value = {
            "session_id": "new-session",
            "status": "awaiting_paystack_otp",
            "order_id": "ORDER-1",
            "payment_reference": "REF-1",
        }
        self.pending.find_one.return_value = {
            "_id": "pending-1",
            "status": "awaiting_paystack_otp",
            "otp_attempts": 0,
            "otp_expires_at": datetime.utcnow() + timedelta(minutes=5),
        }

        with patch.object(
            self.ussd,
            "_submit_paystack_otp",
            return_value=(False, {"status": False}, "Provided registration token is invalid"),
        ):
            result = self.ussd._handle_paystack_otp_input("new-session", "123456")

        self.assertFalse(result["success"])
        self.assertTrue(result["keep_open"])
        self.assertIn("2 attempts left", result["message"])

    def test_send_otp_is_persisted_for_redial(self):
        response = {
            "status": True,
            "data": {
                "status": "send_otp",
                "reference": "REF-1",
                "display_text": "Dial the provider code and enter the generated voucher",
            },
        }

        result = self.ussd._handle_paystack_charge_status("ORDER-1", "session-1", response)

        self.assertEqual("awaiting_paystack_otp", result["session_status"])
        self.assertTrue(result["keep_open"])
        pending_update = self.pending.update_one.call_args_list[-1].args[1]["$set"]
        self.assertEqual("awaiting_paystack_otp", pending_update["status"])
        self.assertIn("otp_expires_at", pending_update)

    def test_other_recipient_requires_matching_number_confirmation(self):
        context = (
            {"_id": "store-1"},
            {"_id": "service-1", "name": "Data", "offers": [{"value_text": "1GB", "total": 5}]},
            {"value_text": "1GB", "total": 5},
            [],
        )
        app = Flask(__name__)

        self.sessions.find_one.return_value = {
            "status": "awaiting_recipient_phone",
            "selected_service_id": "service-1",
            "selected_offer_index": 0,
            "store_id": "store-1",
        }
        with app.app_context(), patch.object(self.ussd, "_session_store", return_value={"_id": "store-1"}), patch.object(
            self.ussd, "_load_store_services", return_value=context[1:2]
        ), patch.object(self.ussd, "_selected_context", return_value=context):
            first_response = self.ussd._handle_arkesel_json(
                {"sessionID": "session-1", "msisdn": "233551111111", "userData": "0551234567"}
            )

        self.assertEqual("Enter recipient number again to confirm", first_response.get_json()["message"])

        self.sessions.find_one.return_value = {
            "status": "awaiting_recipient_phone_confirmation",
            "pending_recipient_phone": "0551234567",
            "selected_service_id": "service-1",
            "selected_offer_index": 0,
            "store_id": "store-1",
        }
        with app.app_context(), patch.object(self.ussd, "_session_store", return_value={"_id": "store-1"}), patch.object(
            self.ussd, "_load_store_services", return_value=context[1:2]
        ), patch.object(self.ussd, "_selected_context", return_value=context):
            mismatch_response = self.ussd._handle_arkesel_json(
                {"sessionID": "session-1", "msisdn": "233551111111", "userData": "0557654321"}
            )

        self.assertIn("Numbers do not match", mismatch_response.get_json()["message"])
        mismatch_update = self.sessions.update_one.call_args.args[1]["$set"]
        self.assertEqual("awaiting_recipient_phone", mismatch_update["status"])

    def test_text_flow_recovers_from_recipient_number_mismatch(self):
        service = {
            "_id": "service-1",
            "name": "Data",
            "offers": [{"value_text": "1GB", "value": "1GB", "amount": 5, "total": 5}],
        }
        store = {"_id": "store-1", "name": "Sir Hans", "slug": "sir-hans"}
        base_text = "AGENT*1*1*2*0551234567*0557654321"

        with patch.object(self.ussd, "_find_agent_code", return_value={"agent_code": "AGENT", "user_id": "user-1"}), patch.object(
            self.ussd, "_is_public_agent_code", return_value=True
        ), patch.object(self.ussd, "_agent_identity", return_value={}), patch.object(
            self.ussd, "_public_store_doc", return_value=store
        ), patch.object(self.ussd, "_load_public_services", return_value=[service]):
            self.sessions.find_one.return_value = {}
            mismatch = self.ussd._handle_text_flow(
                {"sessionId": "session-1", "phoneNumber": "0551111111", "text": base_text}
            )
            marker = self.sessions.update_one.call_args.args[1]["$set"]

            self.sessions.find_one.return_value = {
                "text_recipient_retry_prefix": marker["text_recipient_retry_prefix"],
                "text_recipient_retry_at": marker["text_recipient_retry_at"],
            }
            retry_first = self.ussd._handle_text_flow(
                {"sessionId": "session-1", "phoneNumber": "0551111111", "text": base_text + "*0559999999"}
            )
            retry_match = self.ussd._handle_text_flow(
                {"sessionId": "session-1", "phoneNumber": "0551111111", "text": base_text + "*0559999999*0559999999"}
            )

        self.assertIn("Numbers do not match", mismatch.get_data(as_text=True))
        self.assertIn("again to confirm", retry_first.get_data(as_text=True))
        self.assertIn("To: 0559999999", retry_match.get_data(as_text=True))

    def test_new_session_offers_recent_agent_before_manual_entry(self):
        self.pending.find_one.return_value = None
        self.recent_agents.find_one.return_value = {"msisdn": "0551234567", "agent_code": "AGENT-A"}
        app = Flask(__name__)

        with app.app_context(), patch.object(
            self.ussd, "_find_agent_code", return_value={"agent_code": "AGENT-A", "status": "active"}
        ):
            response = self.ussd._handle_arkesel_json(
                {
                    "sessionID": "new-session",
                    "userID": "user-1",
                    "msisdn": "233551234567",
                    "newSession": True,
                }
            )

        body = response.get_json()
        self.assertTrue(body["continueSession"])
        self.assertEqual("Use recent Agent Code (AGENT-A)?\n1. Yes\n2. No", body["message"])
        session_update = self.sessions.update_one.call_args.args[1]["$set"]
        self.assertEqual("confirming_recent_agent", session_update["status"])

    def test_callback_start_never_returns_hello_only(self):
        self.pending.find_one.return_value = None
        self.recent_agents.find_one.return_value = None
        app = Flask(__name__)
        app.register_blueprint(self.ussd.arkesel_ussd_bp)

        with app.test_client() as client:
            response = client.post(
                "/ussd/arkesel/callback",
                json={
                    "sessionID": "new-session",
                    "userID": "user-1",
                    "msisdn": "233551234567",
                    "serviceCode": "*123#",
                    "network": "MTN",
                    "newSession": True,
                },
            )

        body = response.get_json()
        self.assertEqual("Enter Agent Code", body["message"])
        self.assertNotEqual("Hello", body["message"])

    def test_recent_agent_yes_loads_services_and_no_requests_new_code(self):
        app = Flask(__name__)
        session_doc = {"status": "confirming_recent_agent", "recent_agent_code": "AGENT-A"}
        context = {
            "services": [{"name": "Data"}],
            "store_doc": {"name": "Agent A Store"},
        }

        self.sessions.find_one.return_value = session_doc
        with app.app_context(), patch.object(
            self.ussd, "_activate_agent_session", return_value=(context, "")
        ) as activate:
            yes_response = self.ussd._handle_arkesel_json(
                {"sessionID": "session-1", "msisdn": "0551234567", "userData": "1"}
            )
        activate.assert_called_once_with("session-1", "0551234567", "AGENT-A")
        self.assertIn("Welcome Agent A Store", yes_response.get_json()["message"])

        self.sessions.find_one.return_value = session_doc
        with app.app_context():
            no_response = self.ussd._handle_arkesel_json(
                {"sessionID": "session-2", "msisdn": "0551234567", "userData": "2"}
            )
        self.assertEqual("Enter Agent Code", no_response.get_json()["message"])
        session_update = self.sessions.update_one.call_args.args[1]["$set"]
        self.assertEqual("awaiting_agent_code", session_update["status"])

    def test_valid_agent_code_replaces_recent_agent_for_phone(self):
        context = {
            "agent_code": "AGENT-B",
            "agent_user_id": "user-b",
            "agent_identity": {},
            "is_public": True,
            "store_doc": {"name": "Sir Hans", "slug": "sir-hans"},
            "services": [{"name": "Data"}],
        }

        with patch.object(self.ussd, "_resolve_agent_context", return_value=(context, "")):
            activated, error = self.ussd._activate_agent_session("session-1", "233551234567", "AGENT-B")

        self.assertFalse(error)
        self.assertEqual("AGENT-B", activated["agent_code"])
        recent_update = self.recent_agents.update_one.call_args
        self.assertEqual({"msisdn": "0551234567"}, recent_update.args[0])
        self.assertEqual("AGENT-B", recent_update.args[1]["$set"]["agent_code"])

    def test_text_flow_recent_agent_yes_preserves_later_menu_inputs(self):
        self.pending.find_one.return_value = None
        self.recent_agents.find_one.return_value = {"msisdn": "0551234567", "agent_code": "AGENT-A"}
        service = {
            "name": "Data",
            "offers": [{"value_text": "1GB", "value": "1GB", "amount": 5, "total": 5}],
        }
        context = {
            "agent_code": "AGENT-A",
            "agent_user_id": "user-a",
            "agent_identity": {},
            "is_public": True,
            "store_doc": {"name": "Agent A Store", "slug": "agent-a"},
            "services": [service],
        }

        with patch.object(self.ussd, "_find_agent_code", return_value={"status": "active"}):
            first = self.ussd._handle_text_flow(
                {"sessionId": "session-1", "phoneNumber": "0551234567", "text": ""}
            )

        self.sessions.find_one.return_value = {
            "status": "confirming_recent_agent_text",
            "recent_agent_code": "AGENT-A",
        }
        with patch.object(self.ussd, "_find_agent_code", return_value={"status": "active"}), patch.object(
            self.ussd, "_activate_agent_session", return_value=(context, "")
        ):
            accepted = self.ussd._handle_text_flow(
                {"sessionId": "session-1", "phoneNumber": "0551234567", "text": "1"}
            )
        marker = self.sessions.update_one.call_args.args[1]["$set"]

        self.sessions.find_one.return_value = {
            "status": "selecting_service",
            "text_agent_prefix_code": marker["text_agent_prefix_code"],
            "text_agent_input_offset": marker["text_agent_input_offset"],
        }
        with patch.object(self.ussd, "_activate_agent_session", return_value=(context, "")):
            service_selected = self.ussd._handle_text_flow(
                {"sessionId": "session-1", "phoneNumber": "0551234567", "text": "1*1"}
            )

        self.assertIn("Use recent Agent Code (AGENT-A)", first.get_data(as_text=True))
        self.assertIn("Welcome Agent A Store", accepted.get_data(as_text=True))
        self.assertIn("1GB", service_selected.get_data(as_text=True))


class PaystackChargeRecoveryTests(unittest.TestCase):
    def test_reconciliation_checks_the_charge_api(self):
        paystack, _db = load_paystack_module()
        response = MagicMock(status_code=200, content=b"{}")
        response.json.return_value = {
            "status": True,
            "data": {"status": "pending", "reference": "REF-1"},
        }

        with patch.object(paystack.requests, "get", return_value=response) as get:
            ok, payload, _reason = paystack.verify_paystack_transaction("REF-1")

        self.assertFalse(ok)
        self.assertEqual("pending", payload["status"])
        self.assertEqual("https://api.paystack.co/charge/REF-1", get.call_args.args[0])

    def test_pending_charge_message_is_never_terminal(self):
        paystack, _db = load_paystack_module()

        terminal = paystack._is_terminal_verify_failure(
            {"status": "pending"},
            "Transaction was not completed",
        )

        self.assertFalse(terminal)


if __name__ == "__main__":
    unittest.main()
