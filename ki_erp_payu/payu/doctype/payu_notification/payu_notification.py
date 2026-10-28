# Copyright (c) 2026, kalvyai and contributors
# For license information, please see license.txt

import hashlib
import json

import frappe
from frappe.model.document import Document

from ki_erp_payu.client import redact


class PayUNotification(Document):
	"""Audit log of what PayU sent. The payments themselves are always read from PayU's API."""


def make_event_key(kind, payload):
	parts = [kind] + [
		str(payload.get(key) or "").strip()
		for key in ("mihpayid", "txnid", "status", "unmappedstatus", "request_id", "token", "bank_ref_num")
	]
	return hashlib.sha256("|".join(parts).encode()).hexdigest()


def log_notification(kind, payload, transaction=None, hash_valid=False, result="Recorded", note=None):
	"""Log a notification. Return whether the same notification was already logged.

	Only recorded notifications are deduplicated: a rejected one must never take the key of the
	genuine notification, which would then be ignored as a duplicate.
	"""
	event_key = make_event_key(kind, payload) if result == "Recorded" else None
	if event_key and frappe.db.exists("PayU Notification", {"event_key": event_key}):
		return True
	doc = frappe.get_doc(
		{
			"doctype": "PayU Notification",
			"kind": kind,
			"result": result,
			"hash_valid": hash_valid,
			"note": note,
			"reference": str(payload.get("txnid") or payload.get("merchantTxnId") or "")[:140] or None,
			"payu_transaction": transaction,
			"payu_status": str(payload.get("status") or "")[:140] or None,
			"event_key": event_key,
			"payload": json.dumps(redact(payload), indent=2, default=str),
		}
	)
	try:
		frappe.db.savepoint("payu_notification")
		doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		# The same notification arrived twice at the same time
		frappe.db.rollback(save_point="payu_notification")
		return True
	return False
