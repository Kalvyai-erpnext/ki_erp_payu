import hashlib
import re
from typing import ClassVar
from unittest.mock import MagicMock, patch

from frappe.tests import IntegrationTestCase

from ki_erp_payu import const
from ki_erp_payu.client import (
	PayUClient,
	PayUError,
	clean_text,
	get_message,
	make_action_token,
	make_txnid,
	normalize_phone,
	redact,
)


def sha512(value):
	return hashlib.sha512(value.encode()).hexdigest()


class TestHashes(IntegrationTestCase):
	client = PayUClient("KEY", "SALT")
	fields: ClassVar[dict] = {
		"key": "KEY",
		"txnid": "T1",
		"amount": "10.00",
		"productinfo": "Order",
		"firstname": "Rahul",
		"email": "r@example.test",
		"udf1": "u1",
	}

	def test_request_hash(self):
		# https://docs.payu.in/docs/hashing-request-and-response
		# udf2-udf5 and the 5 reserved fields are empty
		expected = sha512("KEY|T1|10.00|Order|Rahul|r@example.test|u1" + "|" * 10 + "SALT")
		self.assertEqual(self.client.request_hash(self.fields), expected)

	def test_response_hash(self):
		data = {**self.fields, "status": "success"}
		expected = sha512("SALT|success" + "|" * 10 + "u1|r@example.test|Rahul|Order|10.00|T1|KEY")
		self.assertEqual(self.client.response_hash(data), expected)
		self.assertTrue(self.client.is_valid_response({**data, "hash": expected}))
		self.assertTrue(self.client.is_valid_response({**data, "hash": expected.upper()}))

	def test_response_hash_with_additional_charges(self):
		for key in const.ADDITIONAL_CHARGES_KEYS:
			data = {**self.fields, "status": "success", key: "2.50"}
			expected = sha512("2.50|SALT|success" + "|" * 10 + "u1|r@example.test|Rahul|Order|10.00|T1|KEY")
			self.assertEqual(self.client.response_hash(data), expected)

	def test_tampered_response_is_invalid(self):
		data = {**self.fields, "status": "success"}
		data["hash"] = self.client.response_hash(data)
		self.assertFalse(self.client.is_valid_response({**data, "amount": "1.00"}))
		self.assertFalse(self.client.is_valid_response({**data, "status": "failure"}))
		self.assertFalse(self.client.is_valid_response({**data, "hash": ""}))
		self.assertFalse(PayUClient("KEY", "OTHER").is_valid_response(data))
		self.assertFalse(PayUClient("KEY", "").is_valid_response(data))

	def test_command_hash(self):
		self.assertEqual(
			self.client.command_hash("verify_payment", "T1"), sha512("KEY|verify_payment|T1|SALT")
		)


class TestHelpers(IntegrationTestCase):
	def test_txnid(self):
		for name in ("ACC-PAY-REQ-2026-00001", "INV/26-27/0001-1", "", "a" * 60, "ä ö"):
			txnid = make_txnid(name)
			self.assertRegex(txnid, r"^[A-Za-z0-9_-]{1,25}$")
		self.assertNotEqual(make_txnid("X"), make_txnid("X"))
		self.assertTrue(make_txnid("ACC-PAY-REQ-2026-00001").startswith("ACC-PAY-REQ-2026-"))

	def test_action_token(self):
		token = make_action_token("PAYU-RFD-00001")
		self.assertLessEqual(len(token), const.ACTION_TOKEN_MAX_LENGTH)
		self.assertTrue(re.fullmatch(r"[A-Za-z0-9]+", token))
		self.assertNotEqual(token, make_action_token("PAYU-RFD-00001"))

	def test_clean_text(self):
		self.assertEqual(clean_text(" Order | 12\n x ", 100), "Order 12 x")
		self.assertEqual(len(clean_text("a" * 200, 100)), 100)

	def test_normalize_phone(self):
		for phone in ("9876543210", "+91 98765 43210", "+91-9876543210", "09876543210", "919876543210"):
			self.assertEqual(normalize_phone(phone), "9876543210")
		for phone in ("12345", "", None, "+44 7911 123456", "5876543210"):
			self.assertEqual(normalize_phone(phone), "")

	def test_message(self):
		self.assertIsNone(get_message({"error_Message": "No Error"}))
		self.assertEqual(get_message({"error_Message": "E000", "field9": "Bank declined"}), "Bank declined")

	def test_redact(self):
		payload = {"txnid": "T1", "hash": "h", "email": "e", "nested": [{"cardnum": "4111"}]}
		self.assertEqual(
			redact(payload), {"txnid": "T1", "hash": "***", "email": "***", "nested": [{"cardnum": "***"}]}
		)


class TestCommandAPI(IntegrationTestCase):
	def response(self, content=None, status_code=200, text=""):
		response = MagicMock(ok=status_code < 400, status_code=status_code, text=text)
		response.json.side_effect = ValueError if content is None else lambda: content
		return response

	@patch("ki_erp_payu.client.requests.post")
	def test_verify_payments(self, post):
		post.return_value = self.response(
			{
				"status": 1,
				"transaction_details": {
					"T1": {"status": "success", "amt": "10.00"},
					"T2": {"mihpayid": "Not Found", "status": "Not Found"},
				},
			}
		)
		client = PayUClient("KEY", "SALT", "Production")
		self.assertEqual(client.verify_payments(["T1", "T2"]), {"T1": {"status": "success", "amt": "10.00"}})

		url = post.call_args.args[0]
		data = post.call_args.kwargs["data"]
		self.assertEqual(url, const.COMMAND_API_URLS["Production"])
		self.assertEqual(data["var1"], "T1|T2")
		self.assertEqual(data["hash"], sha512("KEY|verify_payment|T1|T2|SALT"))

	@patch("ki_erp_payu.client.requests.post")
	def test_refund_and_actions(self, post):
		client = PayUClient("KEY", "SALT")
		post.return_value = self.response({"status": 1, "request_id": "R1", "error_code": 102})
		client.refund("403993715531", "tok1", 100)
		data = post.call_args.kwargs["data"]
		self.assertEqual(
			(data["command"], data["var1"], data["var2"], data["var3"]),
			("cancel_refund_transaction", "403993715531", "tok1", "100.00"),
		)

		post.return_value = self.response(
			{
				"status": 1,
				"transaction_details": {"403993715531": {"R1": {"action": "refund", "status": "success"}}},
			}
		)
		self.assertEqual(
			client.get_actions("403993715531"), {"R1": {"action": "refund", "status": "success"}}
		)
		self.assertEqual(post.call_args.kwargs["data"]["var2"], "payuid")

	@patch("ki_erp_payu.client.requests.post")
	def test_errors(self, post):
		client = PayUClient("KEY", "SALT")
		post.return_value = self.response(None, text="<html>Bad gateway</html>", status_code=502)
		with self.assertRaises(PayUError):
			client.verify_payments(["T1"])

		post.return_value = self.response({"status": 0, "msg": "Invalid Hash."})
		with self.assertRaisesRegex(PayUError, "Invalid Hash"):
			client.check_credentials()

		post.return_value = self.response(
			{"status": 0, "msg": "0 out of 1 Transactions Fetched Successfully", "transaction_details": {}}
		)
		self.assertIn("Fetched", client.check_credentials())

		with self.assertRaises(PayUError):
			PayUClient("", "").verify_payments(["T1"])
