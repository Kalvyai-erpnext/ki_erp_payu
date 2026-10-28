import json

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import (
	make_payment_request as erpnext_make_payment_request,
)
from frappe import _

from ki_erp_payu import const
from ki_erp_payu.client import PayUError
from ki_erp_payu.payu.doctype.payu_notification.payu_notification import log_notification
from ki_erp_payu.processing import (
	SUCCESS,
	as_administrator,
	commit,
	get_integration_request,
	get_link_status,
	get_return_url,
	get_settings,
	get_transaction,
	sync_integration_request,
	sync_transactions,
)
from ki_erp_payu.refunds import sync_refunds

WEBSHOP_PAYABLE_DOCTYPES = ("Sales Order", "Sales Invoice")
MAKE_PAYMENT_REQUEST = "erpnext.accounts.doctype.payment_request.payment_request.make_payment_request"


def get_payload():
	"""Return what PayU posted, as a form (return page, payment webhook) or as JSON."""
	if frappe.request.is_json:
		data = frappe.request.get_json(silent=True)
		return data if isinstance(data, dict) else None
	data = dict(frappe.form_dict)
	data.pop("cmd", None)
	return data


def redirect(url):
	"""Send the customer coming back from PayU on to `url`, without logging them out.

	PayU posts the customer back from its own site, so the browser doesn't send the session cookie
	(SameSite=Lax) and the request runs as Guest. The cookies Frappe would then set (`sid=Guest`, ...)
	would replace the customer's session: none are sent, and the browser keeps its own for `url`.
	"""
	cookie_manager = getattr(frappe.local, "cookie_manager", None)
	if cookie_manager:
		cookie_manager.cookies.clear()
		cookie_manager.to_delete.clear()
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = url


@frappe.whitelist(allow_guest=True, methods=["POST"])
def payu_return():
	"""Page PayU's checkout posts the customer back to, after a successful or a failed payment.

	The hash proves that PayU produced the data, but the browser could replay old data: the payment
	is recorded only from what PayU's `verify_payment` API returns.
	"""
	data = get_payload() or {}
	txnid = data.get("txnid")
	transaction = get_transaction(txnid)
	if not transaction:
		return redirect(get_return_url(txnid or ""))

	hash_valid = get_settings().get_client().is_valid_response(data)
	log_notification(
		"Customer Return",
		data,
		transaction.name,
		hash_valid=hash_valid,
		result="Recorded" if hash_valid else "Rejected",
		note=None if hash_valid else "Invalid hash: the status is read from PayU's API.",
	)
	try:
		sync_transactions([transaction])
	except PayUError:
		frappe.log_error(title=f"PayU: could not verify {transaction.name}; the scheduler will retry")
	commit()

	ir = get_integration_request(transaction.integration_request)
	result = get_link_status(ir) if ir else {"status": "Pending"}
	if result["status"] == "Completed" and transaction.reload().status == SUCCESS:
		return redirect(result["redirect"])
	return redirect(get_return_url(transaction.name))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook():
	"""Receive PayU's payment webhook (Successful and Failed payment events).

	The hash is checked with the salt; the payment status itself is then read from PayU's API.
	"""
	data = get_payload()
	if data is None:
		frappe.local.response.http_status_code = 400
		return {"status": "invalid"}
	transaction = get_transaction(data.get("txnid") or data.get("merchantTxnId"))
	if not transaction:
		return {"status": "ignored"}

	if not get_settings().get_client().is_valid_response(data):
		log_notification(
			"Payment Webhook",
			data,
			transaction.name,
			result="Rejected",
			note="Invalid hash: the data were not used.",
		)
		commit()
		frappe.local.response.http_status_code = 401
		return {"status": "rejected"}

	if log_notification("Payment Webhook", data, transaction.name, hash_valid=True):
		return {"status": "duplicate"}
	try:
		sync_transactions([transaction])
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"PayU webhook: could not process {transaction.name}")
		# A non-2xx answer makes PayU retry the notification later.
		frappe.local.response.http_status_code = 500
		return {"status": "error"}
	return {"status": transaction.reload().status}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def refund_webhook():
	"""Receive PayU's refund notification, as a trigger only.

	PayU doesn't sign it, so it is never applied: the refunds are read again from PayU's API.
	"""
	data = get_payload()
	if data is None:
		frappe.local.response.http_status_code = 400
		return {"status": "invalid"}
	mihpayid = str(data.get("mihpayid") or "").strip()
	txnid = mihpayid and frappe.db.get_value("PayU Transaction", {"mihpayid": mihpayid, "status": SUCCESS})
	if not txnid:
		return {"status": "ignored"}

	if log_notification(
		"Refund Webhook", data, txnid, note="Unsigned: used to read the refund status from PayU again."
	):
		return {"status": "duplicate"}
	try:
		sync_refunds(txnid)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title=f"PayU refund webhook: could not read the refunds of {txnid}")
		frappe.local.response.http_status_code = 500
		return {"status": "error"}
	return {"status": "received"}


@frappe.whitelist(methods=["POST"])
def check_payment_status(payment_request):
	"""Read the PayU payments and refunds of a Payment Request (button on the form)."""
	frappe.has_permission("Payment Request", "write", payment_request, throw=True)
	names = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": const.SERVICE_NAME,
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request,
		},
		pluck="name",
		order_by="creation desc",
	)
	if not names or not frappe.db.exists("PayU Transaction", {"integration_request": ["in", names]}):
		return _("The customer hasn't opened the PayU checkout yet.")

	statuses = []
	try:
		for name in names:
			statuses.append(sync_integration_request(frappe.get_doc("Integration Request", name))["status"])
		for txnid in frappe.get_all(
			"PayU Transaction",
			filters={"integration_request": ["in", names], "status": SUCCESS},
			pluck="name",
		):
			sync_refunds(txnid)
	except PayUError as e:
		frappe.throw(_("Could not check the payment with PayU: {0}").format(e))

	if "Completed" in statuses:
		return _("The payment is complete.")
	if "Pending" in statuses:
		return _("The payment is still pending at PayU.")
	return _("No successful payment yet: the customer's attempts failed or were cancelled.")


@frappe.whitelist()
def get_refundable_transactions(payment_request):
	"""Return the successful PayU payments of a Payment Request that can still be refunded."""
	frappe.has_permission("Payment Request", "read", payment_request, throw=True)
	return frappe.get_all(
		"PayU Transaction",
		filters={
			"reference_doctype": "Payment Request",
			"reference_docname": payment_request,
			"status": SUCCESS,
		},
		fields=["name", "amount", "refunded_amount", "mihpayid", "mode"],
		order_by="creation desc",
	)


@frappe.whitelist()
def make_payment_request(**args):
	"""Stands in for ERPNext's `make_payment_request` (see `override_whitelisted_methods`).

	Webshop's Pay button calls it with `order_type="Shopping Cart"`: the customer then pays with
	PayU, whatever the Webshop checkout uses otherwise (e.g. Cash on Delivery). ERPNext v16 only
	lets users who can create Payment Requests call it, which shuts out website customers, so their
	access to the order is checked here instead. Every other call goes on unchanged.
	"""
	args = frappe._dict(args)
	if args.order_type != "Shopping Cart" or not frappe.db.get_single_value(
		"PayU Settings", "webshop_pay_button"
	):
		return get_next_make_payment_request()(**args)

	if args.dt not in WEBSHOP_PAYABLE_DOCTYPES or not isinstance(args.dn, str):
		frappe.throw(_("Online payment is only available for orders and invoices."))
	doc = frappe.get_doc(args.dt, args.dn)
	if not (frappe.has_website_permission(doc) or frappe.has_permission(doc.doctype, "read", doc)):
		raise frappe.PermissionError
	if doc.docstatus != 1:
		frappe.throw(_("{0} {1} is not submitted.").format(_(doc.doctype), doc.name))

	gateway_account = frappe.db.get_value(
		"Payment Gateway Account", {"payment_gateway": const.SERVICE_NAME, "currency": doc.currency}
	)
	if not gateway_account:
		frappe.throw(_("Online payment is not available in {0}.").format(doc.currency))

	with as_administrator():
		# Sets the redirect to the PayU checkout page
		return erpnext_make_payment_request(
			dt=doc.doctype,
			dn=doc.name,
			order_type="Shopping Cart",
			payment_gateway_account=gateway_account,
			submit_doc=1,
			loyalty_points=args.loyalty_points,
			return_doc=args.return_doc,
		)


def get_next_make_payment_request():
	"""Return the `make_payment_request` that applies when PayU doesn't handle the call: another
	app's override (e.g. another gateway's Pay button) if there is one, else ERPNext's own."""
	ours = f"{__name__}.make_payment_request"
	overrides = frappe.get_hooks("override_whitelisted_methods", {}).get(MAKE_PAYMENT_REQUEST, [])
	others = [method for method in overrides if method != ours]
	return frappe.get_attr(others[-1]) if others else erpnext_make_payment_request
