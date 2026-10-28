from unittest.mock import patch

import frappe
from frappe.auth import CookieManager

from ki_erp_payu import api
from ki_erp_payu.tests.utils import PayUTestCase, response_data


def notifications(txnid, **filters):
	return frappe.get_all(
		"PayU Notification",
		filters={"payu_transaction": txnid, **filters},
		fields=["kind", "result", "hash_valid", "payload"],
	)


class TestPayUReturn(PayUTestCase):
	def test_return_completes_payment(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)

		_result, response = self.post(api.payu_return, response_data(tx.name, tx.amount))

		self.assertEqual(response.type, "redirect")
		self.assertIn("/payment-success?", response.location)
		self.assertPaid(pr)
		(log,) = notifications(tx.name)
		self.assertEqual((log.kind, log.result, log.hash_valid), ("Customer Return", "Recorded", 1))
		self.assertNotIn("rahul@example.test", log.payload)  # redacted

	def test_return_data_are_not_trusted(self):
		# The browser says "success", PayU's API doesn't know the payment: nothing is recorded
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)

		_result, response = self.post(api.payu_return, response_data(tx.name, tx.amount))

		self.assertEqual(response.location, f"/payu_return?txnid={tx.name}")
		self.assertNotPaid(pr)

	def test_return_with_invalid_hash(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, status="failure", unmappedstatus="failed")
		data = {**response_data(tx.name, tx.amount), "hash": "forged"}

		_result, response = self.post(api.payu_return, data)

		self.assertEqual(response.location, f"/payu_return?txnid={tx.name}")
		self.assertEqual(notifications(tx.name)[0].result, "Rejected")
		self.assertEqual(tx.reload().status, "Failed")  # read from PayU's API all the same

	def test_return_keeps_customer_logged_in(self):
		# The request comes from PayU's site without the session cookie: Frappe runs it as Guest
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)
		previous = getattr(frappe.local, "cookie_manager", None)
		cookies = frappe.local.cookie_manager = CookieManager()
		cookies.cookies["sid"] = {"value": "Guest", "httponly": True}
		cookies.delete_cookie("user_id")
		try:
			self.post(api.payu_return, response_data(tx.name, tx.amount))
		finally:
			frappe.local.cookie_manager = previous
		self.assertEqual((cookies.cookies, cookies.to_delete), ({}, []))
		self.assertPaid(pr)

	def test_unknown_txnid(self):
		_result, response = self.post(api.payu_return, response_data("unknown-txnid", 10))
		self.assertEqual(response.location, "/payu_return?txnid=unknown-txnid")


class TestPayUWebhook(PayUTestCase):
	def test_signed_webhook_completes_payment(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)

		result, _response = self.post(api.webhook, response_data(tx.name, tx.amount))

		self.assertEqual(result, {"status": "Success"})
		self.assertPaid(pr)
		# PayU repeats its notifications
		result, _response = self.post(api.webhook, response_data(tx.name, tx.amount))
		self.assertEqual(result, {"status": "duplicate"})
		self.assertEqual(len(notifications(tx.name, result="Recorded")), 1)

	def test_json_webhook_with_additional_charges(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)
		data = response_data(tx.name, tx.amount, additionalCharges="3.00")

		result, _response = self.post(api.webhook, data, json_body=True)

		self.assertEqual(result, {"status": "Success"})
		self.assertPaid(pr)

	def test_webhook_body_is_not_trusted(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, status="pending", unmappedstatus="pending")

		result, _response = self.post(api.webhook, response_data(tx.name, tx.amount))

		self.assertEqual(result, {"status": "Pending"})
		self.assertNotPaid(pr)

	def test_rejected_webhook(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)

		data = {**response_data(tx.name, tx.amount), "hash": "forged"}
		result, response = self.post(api.webhook, data)

		self.assertEqual((result, response.http_status_code), ({"status": "rejected"}, 401))
		self.assertNotPaid(pr)
		self.assertEqual(notifications(tx.name)[0].result, "Rejected")
		# The genuine notification is still processed afterwards
		result, _response = self.post(api.webhook, response_data(tx.name, tx.amount))
		self.assertEqual(result, {"status": "Success"})

	def test_ignored_webhook(self):
		result, _response = self.post(api.webhook, response_data("unknown-txnid", 10))
		self.assertEqual(result, {"status": "ignored"})

	def test_failure_asks_payu_to_retry(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		with (
			patch("ki_erp_payu.api.sync_transactions", side_effect=Exception("database down")),
			patch("ki_erp_payu.api.frappe.log_error"),
			patch("ki_erp_payu.api.frappe.db.rollback"),  # would undo the test setup
		):
			result, response = self.post(api.webhook, response_data(tx.name, tx.amount))
		self.assertEqual((result, response.http_status_code), ({"status": "error"}, 500))


class TestWebshopPayButton(PayUTestCase):
	def click_pay(self, so, user="rahul@example.test"):
		frappe.set_user(user)
		frappe.local.response = frappe._dict()
		try:
			api.make_payment_request(dt=so.doctype, dn=so.name, submit_doc=1, order_type="Shopping Cart")
		finally:
			frappe.set_user("Administrator")
		return frappe.local.response

	def test_customer_pays_with_payu(self):
		frappe.db.set_single_value(
			"Webshop Settings", "payment_gateway_account", "Cash on Delivery - INR - KD"
		)
		so = self.make_sales_order()

		response = self.click_pay(so)

		self.assertEqual(response.type, "redirect")
		self.assertIn("/payu_checkout?token=", response.location)
		pr = frappe.get_doc("Payment Request", {"reference_name": so.name, "docstatus": 1})
		self.assertEqual(pr.payment_gateway, "PayU")
		self.assertEqual(pr.grand_total, so.grand_total)

	def test_order_of_another_customer(self):
		other = frappe.get_doc(
			{"doctype": "Customer", "customer_name": "PayU Other Customer", "customer_type": "Individual"}
		).insert()
		so = self.make_sales_order()
		so.db_set("customer", other.name)

		with self.assertRaises(frappe.PermissionError):
			self.click_pay(so)
		self.assertFalse(frappe.db.exists("Payment Request", {"reference_name": so.name}))

	def test_other_calls_go_on_unchanged(self):
		with patch("ki_erp_payu.api.get_next_make_payment_request") as get_next:
			api.make_payment_request(dt="Sales Order", dn="SO-1", payment_gateway_account="Any")
		get_next.return_value.assert_called_once_with(
			dt="Sales Order", dn="SO-1", payment_gateway_account="Any"
		)

	def test_pay_button_can_be_left_to_another_gateway(self):
		frappe.db.set_single_value("PayU Settings", "webshop_pay_button", 0)
		try:
			with patch("ki_erp_payu.api.get_next_make_payment_request") as get_next:
				api.make_payment_request(dt="Sales Order", dn="SO-1", order_type="Shopping Cart")
			get_next.return_value.assert_called_once()
		finally:
			frappe.db.set_single_value("PayU Settings", "webshop_pay_button", 1)

	def test_next_override(self):
		other = "other_app.api.make_payment_request"
		hooks = {api.MAKE_PAYMENT_REQUEST: [other, "ki_erp_payu.api.make_payment_request"]}
		with (
			patch("frappe.get_hooks", return_value=hooks),
			patch("frappe.get_attr", side_effect=lambda method: method),
		):
			self.assertEqual(api.get_next_make_payment_request(), other)
		with patch("frappe.get_hooks", return_value={}):
			self.assertIs(api.get_next_make_payment_request(), api.erpnext_make_payment_request)


class TestCheckPaymentStatus(PayUTestCase):
	def test_check_status(self):
		pr = self.make_payment_request()
		self.assertIn("hasn't opened", api.check_payment_status(pr.name))

		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, status="pending", unmappedstatus="pending")
		self.assertIn("pending", api.check_payment_status(pr.name))

		self.pay(tx, status="failure", unmappedstatus="failed")
		self.assertIn("No successful payment", api.check_payment_status(pr.name))

		_ir, _checkout, retry = self.open_checkout(pr)
		self.pay(retry)
		self.assertIn("complete", api.check_payment_status(pr.name))
		self.assertPaid(pr)

	def test_check_status_needs_permission(self):
		pr = self.make_payment_request()
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				api.check_payment_status(pr.name)
		finally:
			frappe.set_user("Administrator")
