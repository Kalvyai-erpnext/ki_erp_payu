# PayU India constants. Every value comes from the PayU India documentation, cited next to it.

SERVICE_NAME = "PayU"

# Hosted checkout: the customer's browser posts the payment form here.
# https://docs.payu.in/docs/prebuilt-checkout-page-integration
CHECKOUT_URLS = {
	"Sandbox": "https://test.payu.in/_payment",
	"Production": "https://secure.payu.in/_payment",
}

# Command ("postservice") API. Responses are JSON with `form=2`.
# https://docs.payu.in/reference/verify_payment_api
COMMAND_API_URLS = {
	"Sandbox": "https://test.payu.in/merchant/postservice.php?form=2",
	"Production": "https://info.payu.in/merchant/postservice.php?form=2",
}

REQUEST_TIMEOUT = 30

# https://docs.payu.in/reference/verify_payment_api
COMMAND_VERIFY_PAYMENT = "verify_payment"
# https://docs.payu.in/reference/refund_transaction_api
COMMAND_REFUND = "cancel_refund_transaction"
# All actions (refunds, captures, cancellations) of a PayU payment, with their tokens.
# https://docs.payu.in/reference/check_action_status_api_with_payu_id
COMMAND_ACTION_STATUS = "check_action_status"

# Request hash: sha512(key|txnid|amount|productinfo|firstname|email|udf1|...|udf5||||||SALT).
# Response hash: sha512(SALT|status||||||udf5|...|udf1|email|firstname|productinfo|amount|txnid|key),
# prefixed with `additional_charges|` when PayU adds a convenience fee.
# https://docs.payu.in/docs/hashing-request-and-response
UDF_KEYS = ("udf1", "udf2", "udf3", "udf4", "udf5")
REQUEST_HASH_KEYS = ("key", "txnid", "amount", "productinfo", "firstname", "email", *UDF_KEYS)
RESPONSE_HASH_KEYS = (
	"status", "", "", "", "", "", *reversed(UDF_KEYS),
	"email", "firstname", "productinfo", "amount", "txnid", "key",
)  # fmt: skip
# Keys under which PayU sends the convenience fee.
# https://docs.payu.in/docs/hashing-request-and-response (`additional_charges`)
# https://docs.payu.in/docs/webhook-events-and-sample-payloads (`additionalCharges`)
ADDITIONAL_CHARGES_KEYS = ("additional_charges", "additionalCharges")

# PayU txnid: unique, at most 25 characters, alphanumerics plus "-" and "_".
# https://docs.payu.in/reference/addl_info-payment-apis
TXNID_MAX_LENGTH = 25
TXNID_SUFFIX_LENGTH = 6
# Lengths of the checkout fields. https://docs.payu.in/docs/prebuilt-checkout-page-integration
FIRSTNAME_MAX_LENGTH = 60
PRODUCTINFO_MAX_LENGTH = 100

# Merchant tokens of refund requests: unique, at most 23 characters.
# https://docs.payu.in/reference/refund_transaction_api
ACTION_TOKEN_MAX_LENGTH = 23

# Several txnids can be verified at once, separated by "|".
# https://docs.payu.in/reference/verify_payment_api
VERIFY_BATCH_SIZE = 50

# PayU India accounts settle in INR, and PayU sends no currency with its payment data.
SUPPORTED_CURRENCIES = ("INR",)

# Indian mobile number, as PayU expects it in `phone`.
INDIAN_MOBILE_PATTERN = r"[6-9]\d{9}"

# Payment `status` and `unmappedstatus` values.
# https://docs.payu.in/reference/payment-state-explanations
STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_PENDING = "pending"
UNMAPPED_AUTHORIZED = "auth"
UNMAPPED_CANCELLED = ("usercancelled", "dropped")

# Statuses of refund actions, compared in lower case.
# https://docs.payu.in/reference/check_action_status_api_with_payu_id
ACTION_STATUS_SUCCESS = ("success",)
ACTION_STATUS_FAILED = ("failure", "failed")
ACTION_STATUS_PENDING = ("queued", "in progress", "requested", "od_hit", "pending")
ACTION_REFUND = "refund"

# Error codes of the refund request.
# https://docs.payu.in/reference/error-codes-for-refund-initiation
REFUND_QUEUED_CODES = ("102",)
REFUND_ERROR_MESSAGES = {
	"105": "Invalid refund amount.",
	"106": "This refund token already exists.",
	"110": "Only one partial refund is allowed on Maestro cards.",
	"111": "The transaction status does not allow a refund.",
	"116": "The transaction was not found at PayU.",
	"128": "Partial refunds are not enabled on this PayU account.",
	"214": "A refund of the same amount was requested less than 5 minutes ago.",
	"226": "The payment was captured today; refund it tomorrow.",
	"227": "A refund with the same amount and token already exists.",
}

# Transactions synchronized by the scheduler: old enough to have reached PayU, recent enough to
# matter.
SYNC_MIN_AGE_MINUTES = 10
SYNC_MAX_AGE_DAYS = 3
SYNC_REFUND_MAX_AGE_DAYS = 30
SYNC_BATCH_SIZE = 200

# Values never written to logs or to the notification log.
SENSITIVE_KEYS = frozenset(
	{
		"hash", "key", "salt", "cardnum", "card_no", "cardnumber", "name_on_card", "email", "phone",
		"firstname", "lastname", "address1", "address2", "zipcode", "vpa", "payerVpa", "field5",
		"card_token", "token",
	}
)  # fmt: skip
