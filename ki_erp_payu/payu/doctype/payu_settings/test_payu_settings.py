# Copyright (c) 2026, kalvyai and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from ki_erp_payu.client import PayUClient, PayUError


class TestPayUSettings(IntegrationTestCase):
	def get_settings(self, **values):
		settings = frappe.get_single("PayU Settings")
		settings.update(
			{"environment": "Sandbox", "merchant_key": "TESTKEY", "merchant_salt": "TESTSALT", **values}
		)
		return settings

	@patch.object(PayUClient, "check_credentials", return_value="0 out of 1 Transactions Fetched")
	def test_save_creates_gateway(self, check_credentials):
		settings = self.get_settings()
		settings.save()

		check_credentials.assert_called_once()
		self.assertTrue(frappe.db.exists("Payment Gateway", "PayU"))
		self.assertTrue(frappe.db.exists("Payment Gateway Account", {"payment_gateway": "PayU"}))
		self.assertTrue(settings.webhook_url.endswith("/api/method/ki_erp_payu.api.webhook"))
		self.assertTrue(settings.refund_webhook_url.endswith("/api/method/ki_erp_payu.api.refund_webhook"))
		self.assertIn("Connected to PayU", settings.test_connection())

	@patch.object(PayUClient, "check_credentials", side_effect=PayUError("Invalid Hash."))
	def test_wrong_credentials_are_rejected(self, _check_credentials):
		with self.assertRaises(frappe.ValidationError):
			self.get_settings(merchant_salt="wrong").save()

	@patch.object(PayUClient, "check_credentials")
	def test_fallback_mobile_must_be_valid(self, _check_credentials):
		with self.assertRaises(frappe.ValidationError):
			self.get_settings(default_phone="12345").save()
		self.get_settings(default_phone="+91 98765 43210").save()

	def test_client_uses_environment(self):
		self.assertIn("secure.payu.in", self.get_settings(environment="Production").get_client().checkout_url)
		self.assertIn("test.payu.in", self.get_settings().get_client().checkout_url)

	def test_only_inr(self):
		settings = self.get_settings()
		settings.validate_transaction_currency("INR")
		with self.assertRaises(frappe.ValidationError):
			settings.validate_transaction_currency("USD")
