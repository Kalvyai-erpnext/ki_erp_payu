from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice
from frappe.utils import flt

from ki_erp_payu import api
from ki_erp_payu.processing import sync_integration_request
from ki_erp_payu.refunds import get_refundable_amount, sync_pending_refunds, sync_refunds
from ki_erp_payu.tests.utils import PayUTestCase

MIHPAYID = "403993715531"


class PayURefundTestCase(PayUTestCase):
	def make_paid_transaction(self, rate=500, dt="Sales Order", dn=None):
		pr = self.make_payment_request(rate=rate, dt=dt, dn=dn)
		ir, _checkout, tx = self.open_checkout(pr)
		self.pay(tx, mihpayid=MIHPAYID)
		sync_integration_request(ir)
		self.assertPaid(pr)
		return tx.reload()

	def make_refund(self, tx, amount, submit=True, **values):
		refund = frappe.get_doc(
			{"doctype": "PayU Refund", "payu_transaction": tx.name, "amount": amount, **values}
		).insert()
		if submit:
			refund.submit()
		return refund

	def refund_entry(self, refund):
		return frappe.get_doc("Payment Entry", refund.reload().payment_entry)


class TestPayURefunds(PayURefundTestCase):
	def test_full_refund(self):
		tx = self.make_paid_transaction()
		refund = self.make_refund(tx, tx.amount, reason="Damaged item")

		self.assertEqual(refund.status, "Queued")
		((mihpayid, token, amount),) = self.fake.refunds
		self.assertEqual((mihpayid, token, amount), (MIHPAYID, refund.token, flt(tx.amount, 2)))
		self.assertLessEqual(len(token), 23)
		self.assertTrue(refund.request_id)
		self.assertEqual(get_refundable_amount(tx.name), 0)
		self.assertFalse(refund.payment_entry)  # nothing is paid back until PayU confirms

		# PayU completes the refund
		self.fake.set_action(MIHPAYID, refund.request_id, "success", tx.amount, token=refund.token)
		self.assertEqual(sync_refunds(tx.name), [refund.name])

		self.assertEqual(refund.reload().status, "Success")
		pe = self.refund_entry(refund)
		original = frappe.get_doc("Payment Entry", tx.payment_entry)
		self.assertEqual(
			(pe.docstatus, pe.payment_type, pe.party, pe.paid_from, pe.paid_to, pe.paid_amount),
			(1, "Pay", original.party, original.paid_to, original.paid_from, flt(tx.amount)),
		)
		self.assertEqual(pe.reference_no, refund.request_id)
		self.assertEqual(tx.reload().refunded_amount, tx.amount)

		# Checking again changes nothing
		self.assertEqual(sync_refunds(tx.name), [])
		self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": refund.request_id}), 1)

	def test_partial_refunds(self):
		tx = self.make_paid_transaction(rate=1000)
		self.make_refund(tx, 300)
		self.assertEqual(get_refundable_amount(tx.name), flt(tx.amount) - 300)

		with self.assertRaisesRegex(frappe.ValidationError, "can still be refunded"):
			self.make_refund(tx, flt(tx.amount) - 299, submit=False)
		self.make_refund(tx, flt(tx.amount) - 300)
		self.assertEqual(get_refundable_amount(tx.name), 0)
		self.assertEqual(len(self.fake.refunds), 2)

	def test_refused_refund(self):
		tx = self.make_paid_transaction()
		self.fake.refund_response = {"status": 0, "msg": "Duplicate", "error_code": 214}
		refund = self.make_refund(tx, 100, submit=False)

		with self.assertRaisesRegex(frappe.ValidationError, "less than 5 minutes ago"):
			refund.submit()
		self.assertEqual(frappe.db.get_value("PayU Refund", refund.name, "docstatus"), 0)

	def test_failed_refund(self):
		tx = self.make_paid_transaction()
		refund = self.make_refund(tx, 100)
		self.fake.set_action(
			MIHPAYID, refund.request_id, "failure", 100, token=refund.token, msg="Bank refused"
		)

		sync_refunds(tx.name)

		refund.reload()
		self.assertEqual((refund.status, refund.message), ("Failed", "Bank refused"))
		self.assertFalse(refund.payment_entry)
		self.assertEqual(get_refundable_amount(tx.name), flt(tx.amount))
		refund.cancel()
		self.assertEqual(refund.reload().status, "Cancelled")

	def test_queued_refund_cannot_be_cancelled(self):
		tx = self.make_paid_transaction()
		refund = self.make_refund(tx, 100)
		with self.assertRaises(frappe.ValidationError):
			refund.cancel()

	def test_only_successful_payments(self):
		pr = self.make_payment_request()
		_ir, _checkout, tx = self.open_checkout(pr)
		with self.assertRaisesRegex(frappe.ValidationError, "Only successful"):
			self.make_refund(tx, 100, submit=False)
		with self.assertRaises(frappe.ValidationError):
			self.make_refund(self.make_paid_transaction(), 0, submit=False)

	def test_dashboard_refund_is_imported(self):
		tx = self.make_paid_transaction()
		self.fake.set_action(MIHPAYID, "DASH1", "success", 120, token="dashtoken")
		self.fake.set_action(MIHPAYID, "CAP1", "success", 120, action="capture")  # not a refund

		(name,) = sync_refunds(tx.name)

		refund = frappe.get_doc("PayU Refund", name)
		self.assertEqual(
			(refund.docstatus, refund.source, refund.status, refund.amount, refund.request_id),
			(1, "PayU Dashboard", "Success", 120, "DASH1"),
		)
		self.assertTrue(refund.payment_entry)
		self.assertEqual(self.fake.refunds, [])  # not requested again
		self.assertEqual(sync_refunds(tx.name), [])  # imported once
		self.assertEqual(get_refundable_amount(tx.name), flt(tx.amount) - 120)

	def test_scheduler_checks_queued_refunds(self):
		tx = self.make_paid_transaction()
		refund = self.make_refund(tx, 100)
		self.fake.set_action(MIHPAYID, refund.request_id, "success", 100, token=refund.token)

		sync_pending_refunds()

		self.assertEqual(refund.reload().status, "Success")
		self.assertTrue(refund.payment_entry)

	def test_payment_entry_can_be_created_again(self):
		tx = self.make_paid_transaction()
		refund = self.make_refund(tx, 100)
		self.fake.set_action(MIHPAYID, refund.request_id, "success", 100, token=refund.token)
		with (
			patch("ki_erp_payu.refunds.make_refund_payment_entry", side_effect=Exception("Closed period")),
			patch("ki_erp_payu.refunds.frappe.log_error"),
		):
			sync_refunds(tx.name)

		refund.reload()
		self.assertEqual(refund.status, "Success")
		self.assertFalse(refund.payment_entry)
		self.assertIn("Closed period", refund.message)

		refund.make_payment_entry()
		self.assertEqual(self.refund_entry(refund).docstatus, 1)
		self.assertFalse(refund.message)


class TestCreditNote(PayURefundTestCase):
	def test_refund_is_allocated_to_credit_note(self):
		so = self.make_sales_order(rate=800)
		si = make_sales_invoice(so.name)
		si.insert()
		si.submit()
		tx = self.make_paid_transaction(dt="Sales Invoice", dn=si.name)
		self.assertEqual(frappe.db.get_value("Sales Invoice", si.name, "outstanding_amount"), 0)
		credit_note = make_sales_return(si.name)
		credit_note.insert()
		credit_note.submit()

		refund = self.make_refund(tx, tx.amount, credit_note=credit_note.name)
		self.fake.set_action(MIHPAYID, refund.request_id, "success", tx.amount, token=refund.token)
		sync_refunds(tx.name)

		pe = self.refund_entry(refund)
		self.assertEqual(pe.payment_type, "Pay")
		self.assertEqual([row.reference_name for row in pe.references], [credit_note.name])
		self.assertEqual(frappe.db.get_value("Sales Invoice", credit_note.name, "outstanding_amount"), 0)

	def test_credit_note_must_match(self):
		tx = self.make_paid_transaction()
		so = self.make_sales_order()
		si = make_sales_invoice(so.name)
		si.insert()
		si.submit()
		with self.assertRaisesRegex(frappe.ValidationError, "not a submitted credit note"):
			self.make_refund(tx, 100, submit=False, credit_note=si.name)


class TestRefundWebhook(PayURefundTestCase):
	def test_refund_webhook_reads_status_from_payu(self):
		tx = self.make_paid_transaction()
		refund = self.make_refund(tx, 100)
		# The webhook body says "success", but PayU's API says the refund is still queued
		payload = {"mihpayid": MIHPAYID, "request_id": refund.request_id, "status": "success"}
		result, _response = self.post(api.refund_webhook, payload, json_body=True)
		self.assertEqual(result, {"status": "received"})
		self.assertEqual(refund.reload().status, "Queued")

		self.fake.set_action(MIHPAYID, refund.request_id, "success", 100, token=refund.token)
		payload["bank_ref_num"] = "RRN1"  # another notification
		self.post(api.refund_webhook, payload, json_body=True)
		self.assertEqual(refund.reload().status, "Success")

		result, _response = self.post(api.refund_webhook, payload, json_body=True)
		self.assertEqual(result, {"status": "duplicate"})

	def test_unknown_payment(self):
		result, _response = self.post(api.refund_webhook, {"mihpayid": "999"}, json_body=True)
		self.assertEqual(result, {"status": "ignored"})
