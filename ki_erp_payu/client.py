"""Small PayU India client: request and response hashes, and the command ("postservice") API.

PayU has no official Python SDK that fits Frappe, so the API is called directly with `requests`.
"""

import hashlib
import hmac
import re
import secrets

import requests
from frappe import _

from ki_erp_payu import const


class PayUError(Exception):
	pass


class PayUClient:
	def __init__(self, key, salt, environment="Sandbox"):
		self.key = key or ""
		self.salt = salt or ""
		self.environment = environment if environment in const.COMMAND_API_URLS else "Sandbox"

	@property
	def checkout_url(self):
		return const.CHECKOUT_URLS[self.environment]

	# === HASHES === #

	def request_hash(self, fields):
		"""Return the hash of the checkout form: sha512(key|txnid|...|udf5||||||SALT)."""
		values = [str(fields.get(key) or "") for key in const.REQUEST_HASH_KEYS]
		return sha512("|".join([*values, "", "", "", "", "", self.salt]))

	def response_hash(self, data):
		"""Return the expected hash of the data PayU posts back (return page and webhook).

		When PayU adds a convenience fee, the hash is prefixed with it:
		`sha512(additional_charges|SALT|status|...|key)`.
		"""
		values = [str(data.get(key) or "") if key else "" for key in const.RESPONSE_HASH_KEYS]
		parts = [self.salt, *values]
		charges = get_additional_charges(data)
		if charges:
			parts.insert(0, charges)
		return sha512("|".join(parts))

	def is_valid_response(self, data):
		received = str(data.get("hash") or "").strip().lower()
		return bool(received and self.salt) and hmac.compare_digest(received, self.response_hash(data))

	def command_hash(self, command, var1):
		"""Return the hash of a command API request: sha512(key|command|var1|SALT)."""
		return sha512(f"{self.key}|{command}|{var1}|{self.salt}")

	# === COMMAND API === #

	def command(self, command, var1, **extra_vars):
		"""Call a PayU command API and return its JSON response.

		:param str command: The command name, e.g. `verify_payment`.
		:param str var1: The first variable, which is part of the hash.
		:param extra_vars: The other variables (`var2`, `var3`, ...).
		:raise PayUError: If PayU cannot be reached or doesn't answer in JSON.
		"""
		if not self.key or not self.salt:
			raise PayUError(_("The PayU merchant key and salt are not configured."))
		data = {
			"key": self.key,
			"command": command,
			"var1": var1,
			"hash": self.command_hash(command, var1),
			**extra_vars,
		}
		try:
			response = requests.post(
				const.COMMAND_API_URLS[self.environment],
				data=data,
				headers={"Accept": "application/json"},
				timeout=const.REQUEST_TIMEOUT,
			)
		except requests.RequestException as e:
			raise PayUError(_("Could not reach PayU: {0}").format(e))
		try:
			content = response.json()
		except ValueError:
			content = None
		if not response.ok or not isinstance(content, dict):
			raise PayUError(
				_("PayU returned an unexpected response ({0}): {1}").format(
					response.status_code, (response.text or "")[:200]
				)
			)
		return content

	def verify_payments(self, txnids):
		"""Return PayU's data of the payments it knows, by txnid.

		See https://docs.payu.in/reference/verify_payment_api
		"""
		response = self.command(const.COMMAND_VERIFY_PAYMENT, "|".join(txnids))
		details = response.get("transaction_details") or {}
		if not isinstance(details, dict):
			return {}
		return {
			txnid: entry
			for txnid, entry in details.items()
			if isinstance(entry, dict) and entry.get("status") not in (None, "", "Not Found")
		}

	def refund(self, mihpayid, token, amount):
		"""Request a refund. PayU only queues it; its outcome is read with `get_actions`.

		See https://docs.payu.in/reference/refund_transaction_api
		"""
		return self.command(const.COMMAND_REFUND, mihpayid, var2=token, var3=f"{amount:.2f}")

	def get_actions(self, mihpayid):
		"""Return the refunds (and other actions) of a payment, by PayU request ID.

		See https://docs.payu.in/reference/check_action_status_api_with_payu_id
		"""
		response = self.command(const.COMMAND_ACTION_STATUS, mihpayid, var2="payuid")
		details = response.get("transaction_details") or {}
		actions = details.get(mihpayid) if isinstance(details, dict) else None
		if not isinstance(actions, dict):
			return {}
		return {key: value for key, value in actions.items() if isinstance(value, dict)}

	def check_credentials(self):
		"""Verify a txnid that doesn't exist. With a valid key and salt, PayU looks the txnid up and
		answers with its (empty) `transaction_details`; otherwise it only answers with an error, such
		as "Invalid Hash.".

		:return: PayU's answer.
		"""
		response = self.command(const.COMMAND_VERIFY_PAYMENT, f"erpnext-check-{secrets.token_hex(4)}")
		if "transaction_details" not in response:
			raise PayUError(response.get("msg") or str(response)[:200])
		return response.get("msg") or ""


def sha512(value):
	return hashlib.sha512(value.encode()).hexdigest().lower()


def is_ok(response):
	"""Return whether a command API call succeeded (`status` 1, sent as a number or as text)."""
	return str(response.get("status")).strip() == "1"


def get_additional_charges(data):
	for key in const.ADDITIONAL_CHARGES_KEYS:
		if data.get(key) not in (None, ""):
			return str(data[key])
	return ""


def get_message(data):
	"""Return PayU's explanation, skipping the "No Error" placeholder PayU sends on failures."""
	for key in ("error_Message", "field9"):
		message = str(data.get(key) or "").strip()
		if message and message.lower() not in ("no error", "e000"):
			return message
	return None


def make_txnid(name):
	"""Return a new PayU txnid for an attempt to pay the document `name`.

	PayU txnids are at most 25 characters, alphanumerics plus "-" and "_", and must be unique: every
	attempt gets its own random suffix, which is never truncated.
	"""
	suffix = f"-{secrets.token_hex(const.TXNID_SUFFIX_LENGTH // 2)}"
	cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name or "").strip("-_") or "erpnext"
	return f"{cleaned[: const.TXNID_MAX_LENGTH - len(suffix)]}{suffix}"


def make_action_token(name):
	"""Return a unique merchant token for a refund request."""
	cleaned = re.sub(r"[^A-Za-z0-9]+", "", name or "")
	return f"{cleaned}x{secrets.token_hex(6)}"[-const.ACTION_TOKEN_MAX_LENGTH :]


def clean_text(value, max_length):
	"""Return a checkout value safe for the hash: "|" separates the hashed fields."""
	return re.sub(r"\s+", " ", str(value or "").replace("|", " ")).strip()[:max_length]


def normalize_phone(phone):
	"""Return a 10-digit Indian mobile number, or an empty string if `phone` isn't one."""
	digits = re.sub(r"\D", "", phone or "").lstrip("0")
	if len(digits) == 12 and digits.startswith("91"):
		digits = digits[2:]
	return digits if re.fullmatch(const.INDIAN_MOBILE_PATTERN, digits) else ""


def redact(payload):
	"""Return a copy of the payload without the values that must never be stored or logged."""
	if isinstance(payload, dict):
		return {
			key: "***" if key in const.SENSITIVE_KEYS else redact(value) for key, value in payload.items()
		}
	if isinstance(payload, list):
		return [redact(value) for value in payload]
	return payload
