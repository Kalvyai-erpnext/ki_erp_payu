import frappe
from frappe import _

from ki_erp_payu.client import PayUError
from ki_erp_payu.processing import get_integration_request, prepare_checkout

no_cache = 1


def get_context(context):
	context.no_cache = 1
	context.show_sidebar = False

	integration_request = get_integration_request(frappe.form_dict.get("token"))
	if not integration_request:
		context.update(status="error", message=_("This payment link is not valid."))
		return

	try:
		checkout = prepare_checkout(integration_request)
	except PayUError as e:
		frappe.log_error(title="PayU: could not open the checkout")
		checkout = {"status": "error", "message": str(e)}

	if checkout["status"] == "paid":
		frappe.local.flags.redirect_location = checkout["redirect"]
		raise frappe.Redirect

	context.update(checkout)
