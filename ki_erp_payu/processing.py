"""Payment flow: open the PayU checkout, read the outcome of the payment and mark the document paid.

One Integration Request is created per payment link (see `PayUSettings.get_payment_url`). Every time
the customer opens the link, a PayU Transaction is created with a new txnid: PayU refuses a txnid
that was already used. The outcome of a payment is always read from PayU's `verify_payment` API,
never from the data the browser or the webhook brings.
"""

import json
from contextlib import contextmanager
from datetime import timedelta
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_url, now_datetime, validate_email_address

from ki_erp_payu import const
from ki_erp_payu.client import (
	PayUError,
	clean_text,
	get_additional_charges,
	get_message,
	make_txnid,
	normalize_phone,
)

# PayU Transaction statuses
INITIATED, PENDING, AUTHORIZED = "Initiated", "Pending", "Authorized"
SUCCESS, FAILED, CANCELLED = "Success", "Failed", "Cancelled"
OPEN_STATUSES = (INITIATED, PENDING, AUTHORIZED)


def commit():
	# The payment outcome must survive errors later in the request; tests roll back instead.
	if not frappe.flags.in_test:
		frappe.db.commit()


def get_settings():
	return frappe.get_single("PayU Settings")


def get_integration_request(token):
	"""Return the Integration Request of a payment link, or None if the token is unknown."""
	if not token or not frappe.db.exists(
		"Integration Request", {"name": token, "integration_request_service": const.SERVICE_NAME}
	):
		return None
	return frappe.get_doc("Integration Request", token)


def get_transaction(txnid):
	if not txnid or not isinstance(txnid, str) or not frappe.db.exists("PayU Transaction", txnid):
		return None
	return frappe.get_doc("PayU Transaction", txnid)


def get_success_url(data, redirect_to=None):
	url = "/payment-success?" + urlencode(
		{"doctype": data.get("reference_doctype"), "docname": data.get("reference_docname")}
	)
	if redirect_to:
		url += "&" + urlencode({"redirect_to": redirect_to})
	return url


def get_return_url(txnid):
	return f"/payu_return?{urlencode({'txnid': txnid})}"


# === CHECKOUT === #


def prepare_checkout(integration_request):
	"""Return what the checkout page needs to post the payment form to PayU.

	:return: A dict with a `status` of "ready" (with the form `action` and `fields`), "paid" (with
	         a `redirect`) or "error" (with a `message`).
	"""
	ir = integration_request
	data = json.loads(ir.data)
	if ir.status == "Cancelled":
		return {"status": "error", "message": _("This payment link has expired.")}
	if ir.status == "Completed":
		return {"status": "paid", "redirect": get_success_url(data)}

	reference = frappe.get_doc(data["reference_doctype"], data["reference_docname"])
	if reference.doctype == "Payment Request":
		if reference.status == "Paid" or flt(reference.outstanding_amount) <= 0:
			return {"status": "paid", "redirect": get_success_url(data)}
		if reference.docstatus != 1 or reference.status == "Cancelled":
			return {"status": "error", "message": _("This payment request is no longer valid.")}

	if (data.get("currency") or "INR") not in const.SUPPORTED_CURRENCIES:
		return {"status": "error", "message": _("PayU only accepts payments in INR.")}

	# An earlier attempt may have succeeded since (e.g. a late UPI confirmation).
	if frappe.db.exists("PayU Transaction", {"integration_request": ir.name, "status": ["!=", INITIATED]}):
		result = sync_integration_request(ir)
		if result["status"] == "Completed":
			return {"status": "paid", "redirect": result["redirect"]}

	settings = get_settings()
	client = settings.get_client()
	amount = flt(data.get("amount"), 2)
	if reference.doctype == "Payment Request":
		amount = flt(reference.outstanding_amount, 2)

	transaction = frappe.get_doc(
		{
			"doctype": "PayU Transaction",
			"txnid": make_txnid(data["reference_docname"]),
			"status": INITIATED,
			"integration_request": ir.name,
			"reference_doctype": data["reference_doctype"],
			"reference_docname": data["reference_docname"],
			"amount": amount,
			"currency": "INR",
		}
	).insert(ignore_permissions=True)
	if ir.status != "Queued":
		ir.db_set("status", "Queued")

	return_url = get_url("/api/method/ki_erp_payu.api.payu_return")
	fields = {
		"key": client.key,
		"txnid": transaction.txnid,
		"amount": f"{amount:.2f}",
		"productinfo": clean_text(
			data.get("description") or data.get("title") or data["reference_docname"],
			const.PRODUCTINFO_MAX_LENGTH,
		),
		"firstname": clean_text(
			data.get("payer_name") or reference.get("party") or _("Customer"), const.FIRSTNAME_MAX_LENGTH
		),
		"email": get_customer_email(data, reference),
		"phone": get_customer_phone(reference, settings),
		"surl": return_url,
		"furl": return_url,
	}
	fields["hash"] = client.request_hash(fields)
	commit()

	return {
		"status": "ready",
		"action": client.checkout_url,
		"fields": fields,
		"amount": amount,
		"currency": "INR",
		"title": data.get("title"),
		"description": data.get("description"),
	}


def get_customer_email(data, reference):
	"""Return the customer's email: PayU rejects payments without one."""
	candidates = [data.get("payer_email"), reference.get("email_to")]
	party = reference.get("party") if reference.get("party_type") == "Customer" else None
	if party:
		candidates.append(frappe.db.get_value("Customer", party, "email_id"))
	for candidate in candidates:
		if candidate and validate_email_address(candidate):
			return candidate
	raise PayUError(
		_("An email address is required to pay with PayU. Please contact us to add your email address.")
	)


def get_customer_phone(reference, settings):
	"""Return the customer's mobile number, falling back on the number set in PayU Settings."""
	candidates = [reference.get("phone_number")]
	if reference.doctype == "Payment Request" and reference.reference_doctype and reference.reference_name:
		meta = frappe.get_meta(reference.reference_doctype)
		fields = [f for f in ("contact_mobile", "contact_phone") if meta.has_field(f)]
		if fields:
			candidates += frappe.db.get_value(
				reference.reference_doctype, reference.reference_name, fields, as_dict=True
			).values()
	party = reference.get("party") if reference.get("party_type") == "Customer" else None
	if party:
		candidates.append(frappe.db.get_value("Customer", party, "mobile_no"))
	candidates.append(settings.default_phone)

	for candidate in candidates:
		phone = normalize_phone(candidate)
		if phone:
			return phone
	raise PayUError(
		_(
			"A valid mobile number is required to pay with PayU. Please contact us to add your mobile "
			"number and try again."
		)
	)


# === PAYMENT OUTCOME === #


def lock_integration_request(name):
	"""Lock the row so that the return page, the webhook and the scheduler can't all record the
	payment. Every attempt of a payment link is recorded under this lock.

	:return: The current status of the Integration Request.
	"""
	try:
		return frappe.db.get_value("Integration Request", name, "status", for_update=True)
	except frappe.QueryDeadlockError:
		# With snapshot isolation (default since MariaDB 11.8), the lock is refused when another
		# request changed the row after this transaction first read it. Nothing has been written
		# yet: start a new transaction, which waits for the other one and reads its outcome.
		frappe.db.rollback()
		return frappe.db.get_value("Integration Request", name, "status", for_update=True)


def sync_transactions(transactions):
	"""Read the payments from PayU's `verify_payment` API and record them.

	:return: The names of the transactions whose status changed.
	"""
	client = get_settings().get_client()
	changed = []
	for start in range(0, len(transactions), const.VERIFY_BATCH_SIZE):
		batch = transactions[start : start + const.VERIFY_BATCH_SIZE]
		payments = client.verify_payments([tx.name for tx in batch])
		for tx in batch:
			tx.db_set("last_sync", now_datetime(), update_modified=False)
			payment = payments.get(tx.name)
			if not payment:
				continue
			frappe.db.savepoint("payu_record")
			try:
				if record_payment(tx.name, payment):
					changed.append(tx.name)
			except Exception:
				frappe.db.rollback(save_point="payu_record")
				frappe.log_error(title=f"PayU: could not record payment {tx.name}")
	return changed


def record_payment(txnid, payment):
	"""Record PayU's data of a payment on its PayU Transaction and mark the document paid.

	:param dict payment: The payment as returned by the `verify_payment` API.
	:return: Whether the status of the transaction changed.
	"""
	ir_name = frappe.db.get_value("PayU Transaction", txnid, "integration_request")
	if ir_name:
		lock_integration_request(ir_name)
	tx = frappe.get_doc("PayU Transaction", txnid, for_update=True)

	status = str(payment.get("status") or "").strip().lower()
	unmapped = str(payment.get("unmappedstatus") or "").strip().lower()
	if (status, unmapped) == (tx.payu_status or "", tx.unmappedstatus or "") and tx.status != INITIATED:
		return False  # nothing new

	message = get_message(payment)
	# A late failure must not overwrite a successful payment.
	if tx.status == SUCCESS and status != const.STATUS_SUCCESS:
		frappe.log_error(
			title=f"PayU: {txnid} reported as {status} after success",
			message=json.dumps(payment, default=str),
		)
		return False

	values = {
		"payu_status": status,
		"unmappedstatus": unmapped,
		"mode": payment.get("mode"),
		"bank_ref_num": payment.get("bank_ref_num"),
		"message": message,
		"net_amount_debit": flt(payment.get("net_amount_debit")),
		"additional_charges": flt(get_additional_charges(payment)),
	}
	mihpayid = str(payment.get("mihpayid") or "").strip()
	if mihpayid and mihpayid != "Not Found":
		values["mihpayid"] = mihpayid
	tx.update(values)

	previous_status = tx.status
	if status == const.STATUS_SUCCESS and unmapped == const.UNMAPPED_AUTHORIZED:
		tx.status = AUTHORIZED
		tx.message = _(
			"PayU only authorised this payment (pre-authorisation). Capture or cancel it from the PayU "
			"dashboard."
		)
	elif status == const.STATUS_SUCCESS:
		error = validate_payment_amount(tx, payment)
		if error:
			tx.status, tx.message = FAILED, error
			frappe.log_error(title="PayU payment amount mismatch", message=error)
		else:
			tx.status = SUCCESS
	elif status == const.STATUS_PENDING:
		tx.status = PENDING
	elif status == const.STATUS_FAILURE and unmapped in const.UNMAPPED_CANCELLED:
		tx.status = CANCELLED
	elif status == const.STATUS_FAILURE:
		tx.status = FAILED
		tx.message = message or _("The payment failed.")
	else:
		frappe.logger("ki_erp_payu").warning(f"Unknown PayU status {status!r} for {txnid}")
	tx.save(ignore_permissions=True)

	if tx.status == SUCCESS and previous_status != SUCCESS:
		complete_payment(tx, ir_name)
	elif tx.status in (FAILED, CANCELLED) and ir_name:
		if frappe.db.get_value("Integration Request", ir_name, "status") == "Queued":
			frappe.db.set_value(
				"Integration Request",
				ir_name,
				{"status": "Failed", "error": json.dumps({"txnid": txnid, "message": tx.message})},
			)
	commit()
	return tx.status != previous_status


def validate_payment_amount(tx, payment):
	"""PayU India settles in INR and sends no currency: the transaction itself must be in INR."""
	received = flt(payment.get("amt") or payment.get("transaction_amount") or payment.get("amount"), 2)
	if received != flt(tx.amount, 2) or tx.currency not in const.SUPPORTED_CURRENCIES:
		return _("PayU transaction {0}: received {1} INR, expected {2} {3}.").format(
			tx.name, received, flt(tx.amount, 2), tx.currency
		)
	return None


def complete_payment(tx, ir_name):
	"""Mark the document of a successful payment as paid and link its Payment Entry."""
	if not ir_name:
		return
	ir = frappe.get_doc("Integration Request", ir_name)
	data = json.loads(ir.data)
	reference = frappe.get_doc(data["reference_doctype"], data["reference_docname"])

	if ir.status == "Completed" or (reference.doctype == "Payment Request" and reference.status == "Paid"):
		# Another attempt of the same link was paid first: the customer paid twice.
		message = _("{0} was already paid. PayU transaction {1} paid it again: refund this payment.").format(
			reference.name, tx.name
		)
		tx.db_set("message", message)
		reference.add_comment("Info", message)
		frappe.log_error(title=f"PayU: {reference.name} paid twice", message=message)
		return

	ir.db_set({"status": "Completed", "output": json.dumps({"txnid": tx.name, "mihpayid": tx.mihpayid})})

	redirect_to = None
	frappe.db.savepoint("payu_authorized")
	try:
		frappe.flags.data = data
		# As Administrator: on ERPNext v16, Webshop's `on_payment_authorized` fails for website
		# users (they can't read the receivable account) and does nothing for guests.
		with as_administrator():
			redirect_to = reference.run_method("on_payment_authorized", "Completed")
	except Exception:
		frappe.db.rollback(save_point="payu_authorized")
		frappe.log_error(title="PayU: on_payment_authorized failed")

	# ERPNext's Payment Request has no `on_payment_authorized` of its own (only Webshop adds one),
	# so the Payment Entry is created here when needed.
	if reference.doctype == "Payment Request":
		reference.reload()
		if reference.status != "Paid" and flt(reference.outstanding_amount) > 0:
			with as_administrator():
				reference.set_as_paid()

		payment_entry = frappe.db.get_value(
			"Payment Entry",
			{"reference_no": reference.name, "docstatus": 1, "payment_type": "Receive"},
			order_by="creation desc",
		)
		values = {"redirect_to": redirect_to}
		if payment_entry:
			values["payment_entry"] = payment_entry
			frappe.db.set_value(
				"Payment Entry",
				payment_entry,
				"remarks",
				_("Paid with PayU: transaction {0}, PayU ID {1} ({2}).").format(
					tx.name, tx.mihpayid, tx.mode or _("unknown method")
				),
			)
		tx.db_set(values)
		reference.add_comment(
			"Info",
			_("Paid with PayU: transaction {0}, PayU ID {1} ({2}).").format(
				tx.name, tx.mihpayid, tx.mode or _("unknown method")
			),
		)
	elif redirect_to:
		tx.db_set("redirect_to", redirect_to)


@contextmanager
def as_administrator():
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		yield
	finally:
		frappe.set_user(user)


def sync_integration_request(ir):
	"""Read the outcome of every attempt to pay a payment link (return page, Check Status button).

	:return: A dict with the `status` ("Completed", "Failed" or "Pending") and, when completed, the
	         `redirect` URL for the customer.
	"""
	transactions = [
		frappe.get_doc("PayU Transaction", name)
		for name in frappe.get_all(
			"PayU Transaction",
			filters={"integration_request": ir.name, "status": ["!=", SUCCESS]},
			pluck="name",
			order_by="creation desc",
			limit=const.VERIFY_BATCH_SIZE,
		)
	]
	if transactions and ir.reload().status != "Completed":
		sync_transactions(transactions)
	return get_link_status(ir.reload())


def get_link_status(ir):
	data = json.loads(ir.data)
	if ir.status == "Completed":
		redirect_to = frappe.db.get_value(
			"PayU Transaction", {"integration_request": ir.name, "status": SUCCESS}, "redirect_to"
		)
		return {"status": "Completed", "redirect": get_success_url(data, redirect_to)}
	latest = frappe.db.get_value(
		"PayU Transaction",
		{"integration_request": ir.name, "status": ["!=", INITIATED]},
		["status", "message"],
		as_dict=True,
		order_by="creation desc",
	)
	if latest and latest.status in (FAILED, CANCELLED):
		return {"status": "Failed", "message": latest.message}
	return {"status": "Pending"}


def sync_pending_payments():
	"""Scheduled job: check recent unfinished payments, in case a webhook was missed or can't arrive.

	Transactions are checked once old enough to have reached PayU, until they are a few days old.
	"""
	if not frappe.db.get_single_value("PayU Settings", "merchant_key"):
		return
	now = now_datetime()
	names = frappe.get_all(
		"PayU Transaction",
		filters={
			"status": ["in", OPEN_STATUSES],
			"creation": [
				"between",
				[
					now - timedelta(days=const.SYNC_MAX_AGE_DAYS),
					add_to_date(now, minutes=-const.SYNC_MIN_AGE_MINUTES),
				],
			],
		},
		pluck="name",
		order_by="creation desc",
		limit=const.SYNC_BATCH_SIZE,
	)
	try:
		sync_transactions([frappe.get_doc("PayU Transaction", name) for name in names])
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="PayU: could not check pending payments")
