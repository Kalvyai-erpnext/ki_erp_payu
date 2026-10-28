"""Refunds: request them from PayU, follow them until PayU reports their outcome, and record them.

PayU only queues a refund. Its outcome is read with the `check_action_status` API, by the
scheduler, the refund webhook (a trigger only: PayU doesn't sign it) or the Check Status button.
A successful refund is recorded with a Payment Entry that pays the money back to the customer.
Refunds made in the PayU dashboard are found by the same API and imported as PayU Refunds.
"""

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
from frappe import _
from frappe.utils import flt, getdate, now_datetime, nowdate

from ki_erp_payu import const
from ki_erp_payu.client import PayUError, is_ok, make_action_token
from ki_erp_payu.processing import SUCCESS, as_administrator, commit, get_settings

# PayU Refund statuses
QUEUED, REFUNDED, FAILED = "Queued", "Success", "Failed"


def get_refundable_amount(txnid, exclude=None):
	"""Return what can still be refunded of a PayU payment: refunds that didn't fail are deducted."""
	tx = frappe.db.get_value("PayU Transaction", txnid, ["status", "amount"], as_dict=True)
	if not tx or tx.status != SUCCESS:
		return 0
	filters = {"payu_transaction": txnid, "docstatus": 1, "status": ["!=", FAILED]}
	if exclude:
		filters["name"] = ["!=", exclude]
	refunded = sum(flt(r) for r in frappe.get_all("PayU Refund", filters=filters, pluck="amount"))
	return flt(flt(tx.amount) - refunded, 2)


def update_refunded_amount(txnid):
	tx = frappe.db.get_value("PayU Transaction", txnid, "amount")
	frappe.db.set_value(
		"PayU Transaction",
		txnid,
		"refunded_amount",
		flt(flt(tx) - get_refundable_amount(txnid), 2),
		update_modified=False,
	)


# === REQUEST === #


def send_refund_request(refund):
	"""Ask PayU to refund, before the refund is submitted. Raise if PayU refuses: the refund then
	stays a draft."""
	mihpayid = frappe.db.get_value("PayU Transaction", refund.payu_transaction, "mihpayid")
	if not mihpayid:
		frappe.throw(_("The PayU ID of transaction {0} is unknown.").format(refund.payu_transaction))
	token = make_action_token(refund.name)
	try:
		response = get_settings().get_client().refund(mihpayid, token, flt(refund.amount, 2))
	except PayUError as e:
		frappe.throw(_("Could not request the refund from PayU: {0}").format(e))

	error_code = str(response.get("error_code") or "")
	if not is_ok(response) or (error_code and error_code not in const.REFUND_QUEUED_CODES):
		detail = const.REFUND_ERROR_MESSAGES.get(error_code) or response.get("msg") or "-"
		frappe.throw(
			_("PayU refused the refund ({0}): {1}").format(error_code or "-", _(detail)),
			title=_("Refund refused"),
		)
	refund.update(
		{
			"status": QUEUED,
			"token": token,
			"request_id": str(response.get("request_id") or response.get("txn_update_id") or ""),
			"bank_ref_num": response.get("bank_ref_num"),
			"message": response.get("msg"),
		}
	)


# === SYNCHRONIZATION === #


def sync_refunds(txnid):
	"""Read the refunds of a PayU payment and record their outcome; import dashboard refunds.

	:return: The names of the refunds that changed or were imported.
	"""
	mihpayid = frappe.db.get_value("PayU Transaction", txnid, "mihpayid")
	if not mihpayid:
		return []
	actions = get_settings().get_client().get_actions(mihpayid)
	changed = []
	for request_id, action in actions.items():
		if str(action.get("action") or "").strip().lower() != const.ACTION_REFUND:
			continue
		action.setdefault("request_id", request_id)
		frappe.db.savepoint("payu_refund")
		try:
			name = find_refund(txnid, action) or import_refund(txnid, action)
			if name and apply_refund_status(name, action):
				changed.append(name)
		except Exception:
			frappe.db.rollback(save_point="payu_refund")
			frappe.log_error(title=f"PayU: could not record a refund of {txnid}")
	if changed:
		update_refunded_amount(txnid)
	commit()
	return changed


def find_refund(txnid, action):
	token = str(action.get("token") or "").strip()
	request_id = str(action.get("request_id") or "").strip()
	for field, value in (("token", token), ("request_id", request_id)):
		if value:
			name = frappe.db.get_value(
				"PayU Refund", {"payu_transaction": txnid, field: value, "docstatus": 1}
			)
			if name:
				return name
	return None


def import_refund(txnid, action):
	"""Record a refund made from the PayU dashboard, so that it's accounted for in ERPNext too."""
	amount = flt(action.get("amt") or action.get("amount"), 2)
	request_id = str(action.get("request_id") or "").strip()
	if amount <= 0 or not request_id:
		return None
	refund = frappe.get_doc(
		{
			"doctype": "PayU Refund",
			"payu_transaction": txnid,
			"amount": amount,
			"source": "PayU Dashboard",
			"status": QUEUED,
			"token": str(action.get("token") or "").strip(),
			"request_id": request_id,
			"reason": _("Refund made in the PayU dashboard."),
		}
	)
	refund.flags.ignore_permissions = True
	refund.flags.payu_imported = True  # already made: don't send it to PayU
	refund.submit()
	return refund.name


def apply_refund_status(name, action):
	"""Record PayU's status of a refund. Return whether it changed."""
	refund = frappe.get_doc("PayU Refund", name, for_update=True)
	payu_status = str(action.get("status") or "").strip().lower()
	if payu_status in const.ACTION_STATUS_PENDING:
		status = QUEUED
	elif payu_status in const.ACTION_STATUS_SUCCESS:
		status = REFUNDED
	elif payu_status in const.ACTION_STATUS_FAILED:
		status = FAILED
	else:
		frappe.logger("ki_erp_payu").warning(f"Unknown PayU refund status {payu_status!r} for {name}")
		return False

	values = {"payu_status": payu_status, "last_sync": now_datetime()}
	if action.get("bank_ref_num"):
		values["bank_ref_num"] = action["bank_ref_num"]
	if action.get("request_id") and not refund.request_id:
		values["request_id"] = str(action["request_id"])
	changed = status != refund.status
	if changed:
		values["status"] = status
		if status == FAILED:
			values["message"] = action.get("msg") or _("PayU reported the refund as failed.")
	refund.db_set(values, update_modified=changed)

	if status == REFUNDED and not refund.payment_entry:
		create_refund_payment_entry(refund.reload())
	return changed


def create_refund_payment_entry(refund):
	"""Record the money paid back to the customer. On failure, the refund keeps its status and
	the Payment Entry can be created again from the refund."""
	frappe.db.savepoint("payu_refund_entry")
	try:
		with as_administrator():
			payment_entry = make_refund_payment_entry(refund)
			payment_entry.insert(ignore_permissions=True)
			payment_entry.submit()
	except Exception as e:
		frappe.db.rollback(save_point="payu_refund_entry")
		frappe.log_error(title=f"PayU: could not create the Payment Entry of refund {refund.name}")
		refund.db_set(
			"message",
			_("PayU refunded the payment, but the Payment Entry could not be created: {0}").format(e),
		)
		return None
	refund.db_set({"payment_entry": payment_entry.name, "message": None})
	return payment_entry


def make_refund_payment_entry(refund):
	"""Return a Payment Entry that pays the refund from the PayU account back to the customer.

	With a credit note, the refund is allocated to it; otherwise it stays unallocated on the
	customer's account, to be reconciled with a credit note later.
	"""
	tx = frappe.get_doc("PayU Transaction", refund.payu_transaction)
	if not tx.payment_entry:
		frappe.throw(_("PayU transaction {0} has no Payment Entry.").format(tx.name))
	original = frappe.get_doc("Payment Entry", tx.payment_entry)
	amount = flt(refund.amount, 2)
	reference_date = getdate(refund.modified) if refund.modified else nowdate()

	if refund.credit_note:
		payment_entry = get_payment_entry("Sales Invoice", refund.credit_note, bank_account=original.paid_to)
		# The outstanding amount of a credit note is negative, and so is what is allocated to it.
		for row in payment_entry.references:
			row.allocated_amount = -amount
		payment_entry.paid_amount = payment_entry.received_amount = amount
	else:
		payment_entry = frappe.new_doc("Payment Entry")
		payment_entry.update(
			{
				"payment_type": "Pay",
				"company": original.company,
				"party_type": original.party_type,
				"party": original.party,
				"paid_from": original.paid_to,
				"paid_to": original.paid_from,
				"paid_amount": amount,
				"received_amount": amount,
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
			}
		)
		payment_entry.setup_party_account_field()
		payment_entry.set_missing_values()

	payment_entry.update(
		{
			"posting_date": nowdate(),
			"mode_of_payment": original.mode_of_payment,
			"reference_no": refund.request_id or refund.name,
			"reference_date": reference_date,
			"remarks": _("PayU refund {0} of transaction {1} (PayU ID {2}), paid by {3}.").format(
				refund.name, tx.name, tx.mihpayid, original.name
			),
		}
	)
	return payment_entry


def sync_pending_refunds():
	"""Scheduled job: read the outcome of the refunds PayU hasn't completed yet."""
	if not frappe.db.get_single_value("PayU Settings", "merchant_key"):
		return
	since = frappe.utils.add_days(now_datetime(), -const.SYNC_REFUND_MAX_AGE_DAYS)
	txnids = frappe.get_all(
		"PayU Refund",
		filters={"docstatus": 1, "status": QUEUED, "creation": [">=", since]},
		pluck="payu_transaction",
		distinct=True,
		limit=const.SYNC_BATCH_SIZE,
	)
	for txnid in txnids:
		try:
			sync_refunds(txnid)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"PayU: could not check the refunds of {txnid}")
