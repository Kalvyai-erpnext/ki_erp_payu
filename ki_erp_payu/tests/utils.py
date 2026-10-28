from unittest.mock import patch
from urllib.parse import urlencode

import frappe
from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from ki_erp_payu import const
from ki_erp_payu.client import PayUClient
from ki_erp_payu.processing import prepare_checkout

# Demo data of the development site
COMPANY = "Kalvy Demo"
CUSTOMER = "Rahul Sharma"
ITEM = "TSHIRT-001"
WAREHOUSE = "Stores - KD"
KEY = "TESTKEY"
SALT = "TESTSALT"


class FakePayU:
	"""Stands in for PayU's command API: returns preset payments and remembers refunds."""

	def __init__(self):
		self.payments = {}  # txnid -> verify_payment entry
		self.actions = {}  # mihpayid -> {request_id: action}
		self.refunds = []  # (mihpayid, token, amount) of the refund requests
		self.refund_response = None

	def verify_payments(self, client, txnids):
		return {txnid: dict(self.payments[txnid]) for txnid in txnids if txnid in self.payments}

	def refund(self, client, mihpayid, token, amount):
		self.refunds.append((mihpayid, token, amount))
		if self.refund_response:
			return self.refund_response
		request_id = f"R{len(self.refunds)}{mihpayid}"
		self.set_action(mihpayid, request_id, amt=amount, token=token, status="queued")
		return {"status": 1, "msg": "Refund Request Queued", "request_id": request_id, "error_code": 102}

	def get_actions(self, client, mihpayid):
		return {key: dict(value) for key, value in self.actions.get(mihpayid, {}).items()}

	def set_action(self, mihpayid, request_id, status, amt, token="", action="refund", **extra):
		self.actions.setdefault(mihpayid, {})[request_id] = {
			"action": action,
			"amt": f"{amt:.2f}",
			"token": token,
			"status": status,
			"request_id": request_id,
			"mihpayid": mihpayid,
			**extra,
		}


def response_data(txnid, amount, status="success", **extra):
	"""Return signed data as PayU posts them to the return page and the webhook."""
	data = {
		"key": KEY,
		"txnid": txnid,
		"amount": f"{amount:.2f}",
		"productinfo": "Order",
		"firstname": "Rahul",
		"email": "rahul@example.test",
		"status": status,
		"mihpayid": "403993715531",
		**extra,
	}
	data["hash"] = PayUClient(KEY, SALT).response_hash(data)
	return data


class PayUTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		settings = frappe.get_single("PayU Settings")
		settings.update(
			{
				"environment": "Sandbox",
				"merchant_key": KEY,
				"merchant_salt": SALT,
				"default_phone": "",
				"webshop_pay_button": 1,
			}
		)
		settings.flags.ignore_mandatory = True  # skips the credential check with PayU
		settings.save()
		cls.gateway_account = frappe.db.get_value(
			"Payment Gateway Account", {"payment_gateway": const.SERVICE_NAME, "currency": "INR"}
		)

	def setUp(self):
		self.fake = FakePayU()
		for method in ("verify_payments", "refund", "get_actions"):
			patcher = patch.object(PayUClient, method, autospec=True, side_effect=getattr(self.fake, method))
			patcher.start()
			self.addCleanup(patcher.stop)
		# No test may reach PayU
		guard = patch("ki_erp_payu.client.requests.post", side_effect=AssertionError("PayU was called"))
		guard.start()
		self.addCleanup(guard.stop)

	def make_sales_order(self, rate=500):
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": COMPANY,
				"customer": CUSTOMER,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 7),
				"items": [{"item_code": ITEM, "qty": 1, "rate": rate, "warehouse": WAREHOUSE}],
			}
		).insert()
		so.submit()
		return so

	def make_payment_request(self, rate=500, dt="Sales Order", dn=None):
		if not dn:
			dn = self.make_sales_order(rate).name
		return make_payment_request(
			dt=dt,
			dn=dn,
			payment_gateway_account=self.gateway_account,
			mute_email=1,
			submit_doc=1,
			return_doc=1,
		)

	def open_checkout(self, pr):
		"""Open the payment link of `pr`. Return the Integration Request, the checkout and the new
		PayU Transaction."""
		token = pr.payment_url.split("token=")[1]
		ir = frappe.get_doc("Integration Request", token)
		checkout = prepare_checkout(ir)
		tx = None
		if checkout["status"] == "ready":
			tx = frappe.get_doc("PayU Transaction", checkout["fields"]["txnid"])
		return ir.reload(), checkout, tx

	def pay(self, tx, status="success", unmappedstatus="captured", amount=None, mihpayid=None, **extra):
		"""Make PayU's API report a payment for the PayU Transaction `tx`."""
		self.fake.payments[tx.name] = {
			"mihpayid": mihpayid or f"40399{len(self.fake.payments) + 1:07d}",
			"status": status,
			"unmappedstatus": unmappedstatus,
			"amt": f"{tx.amount if amount is None else amount:.2f}",
			"mode": "UPI",
			"bank_ref_num": "BANKREF1",
			"error_Message": "No Error",
			"net_amount_debit": f"{tx.amount if amount is None else amount:.2f}",
			**extra,
		}

	def post(self, method, data, json_body=False):
		"""Call a public endpoint as PayU would. Return its result and HTTP status."""
		if json_body:
			environ = EnvironBuilder(method="POST", json=data).get_environ()
		else:
			environ = EnvironBuilder(
				method="POST",
				data=urlencode(data),
				content_type="application/x-www-form-urlencoded",
			).get_environ()
		previous_request = getattr(frappe.local, "request", None)
		previous_form = frappe.local.form_dict
		frappe.local.request = Request(environ)
		frappe.local.form_dict = frappe._dict({} if json_body else data)
		frappe.local.response = frappe._dict()
		try:
			result = method()
		finally:
			frappe.local.request = previous_request
			frappe.local.form_dict = previous_form
		return result, frappe.local.response

	def assertPaid(self, pr, payment_entries=1):
		self.assertEqual(frappe.db.get_value("Payment Request", pr.name, "status"), "Paid")
		self.assertEqual(
			frappe.db.count("Payment Entry", {"reference_no": pr.name, "docstatus": 1}), payment_entries
		)

	def assertNotPaid(self, pr):
		self.assertNotEqual(frappe.db.get_value("Payment Request", pr.name, "status"), "Paid")
		self.assertEqual(frappe.db.count("Payment Entry", {"reference_no": pr.name}), 0)
