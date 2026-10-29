app_name = "ki_erp_payu"
app_title = "PayU Payments for ERPNext"
app_publisher = "kalvyai"
app_description = "PayU India payment gateway for ERPNext: verified payments and refunds"
app_email = "kalvyai.admin@gmail.com"
app_license = "Proprietary"

required_apps = ["frappe/erpnext", "frappe/payments"]

doctype_js = {"Payment Request": "public/js/payment_request.js"}

# The Webshop's Pay button: payment with PayU, open to website customers
override_whitelisted_methods = {
	"erpnext.accounts.doctype.payment_request.payment_request.make_payment_request": (
		"ki_erp_payu.api.make_payment_request"
	)
}

scheduler_events = {
	"cron": {
		"*/15 * * * *": [
			"ki_erp_payu.processing.sync_pending_payments",
			"ki_erp_payu.refunds.sync_pending_refunds",
		],
	},
}
