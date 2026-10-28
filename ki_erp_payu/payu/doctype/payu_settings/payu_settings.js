// Copyright (c) 2026, kalvyai and contributors
// For license information, please see license.txt

frappe.ui.form.on("PayU Settings", {
	refresh(frm) {
		if (frm.doc.merchant_key && !frm.is_dirty()) {
			frm.add_custom_button(__("Test Connection"), () => {
				frm.call("test_connection").then((r) => {
					frappe.show_alert({ message: r.message, indicator: "green" });
				});
			});
		}
		for (const [field, label] of [
			["webhook_url", __("Copy Payment Webhook URL")],
			["refund_webhook_url", __("Copy Refund Webhook URL")],
		]) {
			if (frm.doc[field]) {
				frm.add_custom_button(
					label,
					() => frappe.utils.copy_to_clipboard(frm.doc[field]),
					__("Webhooks")
				);
			}
		}
		frm.add_custom_button(__("Transactions"), () => frappe.set_route("List", "PayU Transaction"), __("View"));
		frm.add_custom_button(__("Refunds"), () => frappe.set_route("List", "PayU Refund"), __("View"));
		frm.add_custom_button(__("Notifications"), () => frappe.set_route("List", "PayU Notification"), __("View"));
	},
});
