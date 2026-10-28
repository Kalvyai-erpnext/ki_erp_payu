import frappe
from frappe import _

from ki_erp_payu.client import PayUError
from ki_erp_payu.processing import get_integration_request, get_transaction, sync_integration_request

no_cache = 1


def get_context(context):
	"""Result page after PayU's checkout; the outcome is read from PayU's API."""
	context.no_cache = 1
	context.show_sidebar = False

	transaction = get_transaction(frappe.form_dict.get("txnid"))
	integration_request = transaction and get_integration_request(transaction.integration_request)
	if not integration_request:
		context.update(status="error", message=_("This payment could not be found."))
		return

	try:
		result = sync_integration_request(integration_request)
	except PayUError:
		frappe.log_error(title=f"PayU: could not check transaction {transaction.name}")
		result = {"status": "Pending"}

	if result["status"] == "Completed":
		frappe.local.flags.redirect_location = result["redirect"]
		raise frappe.Redirect

	context.status = result["status"]
	context.message = result.get("message")
	context.retry_url = f"/payu_checkout?token={integration_request.name}"
