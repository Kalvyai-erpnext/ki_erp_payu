# Copyright (c) 2026, kalvyai and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

from ki_erp_payu.client import PayUError
from ki_erp_payu.processing import SUCCESS
from ki_erp_payu.refunds import (
	FAILED,
	QUEUED,
	REFUNDED,
	create_refund_payment_entry,
	get_refundable_amount,
	send_refund_request,
	sync_refunds,
	update_refunded_amount,
)


class PayURefund(Document):
	@property
	def refundable_amount(self):
		if not self.payu_transaction:
			return 0
		return get_refundable_amount(self.payu_transaction, exclude=None if self.is_new() else self.name)

	def validate(self):
		tx = frappe.db.get_value(
			"PayU Transaction",
			self.payu_transaction,
			["status", "mihpayid", "payment_entry"],
			as_dict=True,
		)
		if not tx or tx.status != SUCCESS or not tx.mihpayid:
			frappe.throw(_("Only successful PayU payments can be refunded."))
		self.mihpayid = tx.mihpayid
		if tx.payment_entry:
			self.party_type, self.party = frappe.db.get_value(
				"Payment Entry", tx.payment_entry, ["party_type", "party"]
			)
		if self.docstatus == 0:
			self.status = "Draft"
		if self.flags.payu_imported:
			return  # already made in the PayU dashboard

		self.amount = flt(self.amount, 2)
		if self.amount <= 0:
			frappe.throw(_("The refund amount must be greater than zero."))
		if self.amount > self.refundable_amount:
			frappe.throw(
				_("At most {0} can still be refunded on PayU transaction {1}.").format(
					frappe.format(self.refundable_amount, {"fieldtype": "Currency", "options": "INR"}),
					self.payu_transaction,
				)
			)
		self.validate_credit_note()

	def validate_credit_note(self):
		if not self.credit_note:
			return
		note = frappe.db.get_value(
			"Sales Invoice",
			self.credit_note,
			["is_return", "docstatus", "customer", "outstanding_amount"],
			as_dict=True,
		)
		if not note or not note.is_return or note.docstatus != 1:
			frappe.throw(_("{0} is not a submitted credit note.").format(self.credit_note))
		if self.party_type == "Customer" and note.customer != self.party:
			frappe.throw(_("Credit note {0} is not for {1}.").format(self.credit_note, self.party))
		if self.amount > abs(flt(note.outstanding_amount)):
			frappe.throw(
				_("The refund amount is more than what is outstanding on credit note {0}.").format(
					self.credit_note
				)
			)

	def before_submit(self):
		if not self.flags.payu_imported:
			send_refund_request(self)

	def on_submit(self):
		update_refunded_amount(self.payu_transaction)

	def before_cancel(self):
		if self.status != FAILED:
			frappe.throw(_("Only refunds that PayU reports as failed can be cancelled."))

	def on_cancel(self):
		self.db_set("status", "Cancelled")
		update_refunded_amount(self.payu_transaction)

	@frappe.whitelist()
	def check_status(self):
		"""Read the outcome of the refund from PayU (button on the form)."""
		frappe.has_permission("PayU Refund", "read", self, throw=True)
		frappe.only_for(("System Manager", "Accounts Manager", "Accounts User"))
		try:
			changed = sync_refunds(self.payu_transaction)
		except PayUError as e:
			frappe.throw(_("Could not check the refund with PayU: {0}").format(e))
		if self.name in changed:
			return _("Updated from PayU.")
		return _("PayU has nothing new for this refund yet.")

	@frappe.whitelist()
	def make_payment_entry(self):
		"""Create the Payment Entry of a successful refund again, after it failed."""
		frappe.has_permission("PayU Refund", "submit", self, throw=True)
		if self.status != REFUNDED or self.payment_entry:
			frappe.throw(_("This refund has no Payment Entry to create."))
		payment_entry = create_refund_payment_entry(self)
		if not payment_entry:
			frappe.throw(self.reload().message)
		return payment_entry.name
