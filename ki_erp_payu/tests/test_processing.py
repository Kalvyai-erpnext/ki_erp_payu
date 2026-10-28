import json

import frappe
from frappe.utils import add_to_date, flt, now_datetime

from ki_erp_payu.client import PayUClient, PayUError
from ki_erp_payu.processing import (
	get_customer_phone,
	prepare_checkout,
	sync_integration_request,
	sync_pending_payments,
	sync_transactions,
)
from ki_erp_payu.tests.utils import KEY, SALT, PayUTestCase


class TestPayUCheckout(PayUTestCase):
	def test_payment_link(self):
		self.assertTrue(self.gateway_account)
		pr = self.make_payment_request()
		self.assertIn("/payu_checkout?token=", pr.payment_url)
		# Asking again gives the same link
		self.assertEqual(pr.get_payment_url(), pr.payment_url)

	def test_checkout_form(self):
		pr = self.make_payment_request(rate=750)
		ir, checkout, tx = self.open_checkout(pr)

		self.assertEqual(checkout["status"], "ready")
		self.assertEqual(checkout["action"], "https://test.payu.in/_payment")
		fields = checkout["fields"]
		self.assertEqual(fields["key"], KEY)
		self.assertEqual(fields["amount"], f"{flt(pr.grand_total, 2):.2f}")
		self.assertEqual(fields["phone"], "9876543210")
		self.assertEqual(fields["email"], "rahul@example.test")
		self.assertEqual(fields["firstname"], "Rahul Sharma")
		self.assertTrue(fields["surl"].endswith("/api/method/ki_erp_payu.api.payu_return"))
		self.assertEqual(fields["hash"], PayUClient(KEY, SALT).request_hash(fields))
		self.assertRegex(fields["txnid"], r"^[A-Za-z0-9_-]{1,25}$")

		self.assertEqual(ir.status, "Queued")
		self.assertEqual(
			(tx.status, tx.amount, tx.integration_request), ("Initiated", pr.grand_total, ir.name)
		)
		self.assertEqual((tx.reference_doctype, tx.reference_docname), ("Payment Request", pr.name))

	def test_every_opening_gets_a_new_txnid(self):
		pr = self.make_payment_request()
		_ir, first, _tx = self.open_checkout(pr)
		_ir, second, _tx = self.open_checkout(pr)
		self.assertNotEqual(first["fields"]["txnid"], second["fields"]["txnid"])

	def test_cancelled_payment_request(self):
		pr = self.make_payment_request()
		pr.cancel()
		_ir, checkout, _tx = self.open_checkout(pr)
		self.assertEqual(checkout["status"], "error")

	def test_paid_link_redirects(self):
		pr = self.make_payment_request()
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)
		sync_integration_request(ir)

		checkout = prepare_checkout(ir.reload())
		self.assertEqual(checkout["status"], "paid")
		self.assertIn(f"docname={pr.name}", checkout["redirect"])

	def test_late_payment_of_an_earlier_attempt(self):
		pr = self.make_payment_request()
		ir, _checkout, first = self.open_checkout(pr)
		self.pay(first, status="pending", unmappedstatus="pending")
		sync_integration_request(ir)
		# PayU confirms the first attempt when the customer opens the link again
		self.pay(first)
		checkout = prepare_checkout(ir.reload())
		self.assertEqual(checkout["status"], "paid")
		self.assertPaid(pr)

	def test_phone_and_email_are_required(self):
		pr = self.make_payment_request()
		pr.db_set("phone_number", "12345")
		pr.reload()
		self.assertEqual(get_customer_phone(pr, frappe.get_single("PayU Settings")), "9876543210")

		customer = frappe.get_doc(
			{"doctype": "Customer", "customer_name": "PayU No Contact", "customer_type": "Individual"}
		).insert()
		so = self.make_sales_order()
		so.db_set({"customer": customer.name, "contact_mobile": None, "contact_phone": None})
		pr = self.make_payment_request(dn=so.name)
		pr.db_set({"party": customer.name, "email_to": None, "phone_number": None})
		ir = frappe.get_doc("Integration Request", pr.payment_url.split("token=")[1])
		data = json.loads(ir.data)
		data["payer_email"] = "Administrator"  # what ERPNext sends when there is no email
		ir.db_set("data", json.dumps(data))
		with self.assertRaisesRegex(PayUError, "email"):
			prepare_checkout(ir)
		data["payer_email"] = "nocontact@example.test"
		ir.db_set("data", json.dumps(data))
		with self.assertRaisesRegex(PayUError, "mobile"):
			prepare_checkout(ir)
		frappe.db.set_single_value("PayU Settings", "default_phone", "9123456780")
		try:
			self.assertEqual(prepare_checkout(ir)["fields"]["phone"], "9123456780")
		finally:
			frappe.db.set_single_value("PayU Settings", "default_phone", "")


class TestPayUOutcome(PayUTestCase):
	def test_successful_payment(self):
		pr = self.make_payment_request()
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, mihpayid="403993715531", additional_charges="5.00")

		result = sync_integration_request(ir)

		self.assertEqual(result["status"], "Completed")
		self.assertPaid(pr)
		tx.reload()
		self.assertEqual(
			(tx.status, tx.mihpayid, tx.mode, tx.bank_ref_num, tx.additional_charges),
			("Success", "403993715531", "UPI", "BANKREF1", 5),
		)
		self.assertIsNone(tx.message)  # PayU's "No Error" is skipped
		self.assertEqual(
			tx.payment_entry, frappe.db.get_value("Payment Entry", {"reference_no": pr.name, "docstatus": 1})
		)
		self.assertIn("403993715531", frappe.db.get_value("Payment Entry", tx.payment_entry, "remarks"))
		self.assertEqual(ir.reload().status, "Completed")

		# Checking again changes nothing
		self.assertEqual(sync_integration_request(ir)["status"], "Completed")
		self.assertPaid(pr)

	def test_amount_mismatch(self):
		pr = self.make_payment_request()
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, amount=1)

		self.assertEqual(sync_integration_request(ir)["status"], "Failed")
		self.assertNotPaid(pr)
		self.assertIn("expected", tx.reload().message)

	def test_failed_payment_can_be_retried(self):
		pr = self.make_payment_request()
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, status="failure", unmappedstatus="failed", field9="Insufficient funds")

		result = sync_integration_request(ir)

		self.assertEqual(result, {"status": "Failed", "message": "Insufficient funds"})
		self.assertEqual(ir.reload().status, "Failed")
		self.assertNotPaid(pr)
		# The same link opens a new attempt, which can succeed
		self.assertEqual(pr.get_payment_url(), pr.payment_url)
		_ir, checkout, retry = self.open_checkout(pr)
		self.assertEqual(checkout["status"], "ready")
		self.pay(retry)
		self.assertEqual(sync_integration_request(ir)["status"], "Completed")
		self.assertPaid(pr)

	def test_cancelled_by_customer(self):
		pr = self.make_payment_request()
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, status="failure", unmappedstatus="userCancelled")
		self.assertEqual(sync_integration_request(ir)["status"], "Failed")
		self.assertEqual(tx.reload().status, "Cancelled")

	def test_pending_and_authorized_payments_are_not_paid(self):
		pr = self.make_payment_request()
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, status="pending", unmappedstatus="pending")
		self.assertEqual(sync_integration_request(ir)["status"], "Pending")
		self.assertEqual(tx.reload().status, "Pending")

		self.pay(tx, status="success", unmappedstatus="auth")
		self.assertEqual(sync_integration_request(ir)["status"], "Pending")
		self.assertEqual(tx.reload().status, "Authorized")
		self.assertNotPaid(pr)

	def test_late_failure_doesnt_undo_payment(self):
		pr = self.make_payment_request()
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)
		sync_integration_request(ir)
		self.pay(tx, status="failure", unmappedstatus="failed")
		sync_transactions([tx.reload()])
		self.assertEqual(tx.reload().status, "Success")
		self.assertPaid(pr)

	def test_paid_twice(self):
		pr = self.make_payment_request()
		ir, _checkout, first = self.open_checkout(pr)
		_ir, _checkout, second = self.open_checkout(pr)
		self.pay(first)
		self.pay(second)

		sync_integration_request(ir)

		self.assertPaid(pr, payment_entries=1)
		statuses = {first.reload().status, second.reload().status}
		self.assertEqual(statuses, {"Success"})
		messages = [first.message or "", second.message or ""]
		self.assertEqual(sum("refund this payment" in m for m in messages), 1)

	def test_scheduler_checks_pending_payments(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx)

		sync_pending_payments()  # too recent: PayU may not have it yet
		self.assertNotPaid(pr)

		tx.db_set("creation", add_to_date(now_datetime(), minutes=-30))
		sync_pending_payments()
		self.assertPaid(pr)
		self.assertTrue(tx.reload().last_sync)

		old = self.make_payment_request()
		_ir, _checkout, old_tx = self.open_checkout(old)
		self.pay(old_tx)
		old_tx.db_set("creation", add_to_date(now_datetime(), days=-4))
		sync_pending_payments()  # too old
		self.assertNotPaid(old)
