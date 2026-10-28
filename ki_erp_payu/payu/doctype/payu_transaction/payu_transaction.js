// Copyright (c) 2026, kalvyai and contributors
// For license information, please see license.txt

frappe.ui.form.on("PayU Transaction", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(__("Check Status with PayU"), () => {
			frm.call({ method: "check_status", doc: frm.doc, freeze: true }).then((r) => {
				frappe.show_alert({ message: r.message, indicator: "blue" });
				frm.reload_doc();
			});
		});
		const refundable = (frm.doc.__onload || {}).refundable_amount;
		if (frm.doc.status === "Success" && refundable > 0 && frappe.model.can_create("PayU Refund")) {
			frm.add_custom_button(__("Refund"), () => {
				frappe.new_doc("PayU Refund", { payu_transaction: frm.doc.name, amount: refundable });
			});
		}
	},
});
