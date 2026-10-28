# Copyright (c) 2026, kalvyai and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from ki_erp_payu.client import PayUError
from ki_erp_payu.processing import SUCCESS, sync_transactions
from ki_erp_payu.refunds import get_refundable_amount, sync_refunds


class PayUTransaction(Document):
	def onload(self):
		self.set_onload("refundable_amount", get_refundable_amount(self.name))

	@frappe.whitelist()
	def check_status(self):
		"""Read the payment and its refunds from PayU (button on the form)."""
		frappe.has_permission("PayU Transaction", "read", self, throw=True)
		frappe.only_for(("System Manager", "Accounts Manager", "Accounts User"))
		try:
			changed = sync_transactions([self])
			refunds = sync_refunds(self.name) if self.reload().status == SUCCESS else []
		except PayUError as e:
			frappe.throw(_("Could not check the transaction with PayU: {0}").format(e))
		if changed or refunds:
			return _("Updated from PayU.")
		return _("PayU has nothing new for this transaction.")
