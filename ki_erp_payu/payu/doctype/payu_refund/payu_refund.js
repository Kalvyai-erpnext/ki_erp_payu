// Copyright (c) 2026, kalvyai and contributors
// For license information, please see license.txt

frappe.ui.form.on("PayU Refund", {
	setup(frm) {
		frm.set_query("payu_transaction", () => ({ filters: { status: "Success" } }));
		frm.set_query("credit_note", () => ({
			filters: {
				is_return: 1,
				docstatus: 1,
				outstanding_amount: ["<", 0],
				...(frm.doc.party_type === "Customer" && frm.doc.party ? { customer: frm.doc.party } : {}),
			},
		}));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			// set_intro replaces the message, so refreshing the form doesn't repeat it
			frm.set_intro(
				__("Submitting sends the refund to PayU. PayU then processes it, which can take a few days."),
				"blue"
			);
		}
		if (frm.doc.docstatus === 1 && frm.doc.status === "Queued") {
			frm.add_custom_button(__("Check Status with PayU"), () => {
				frm.call({ method: "check_status", doc: frm.doc, freeze: true }).then((r) => {
					frappe.show_alert({ message: r.message, indicator: "blue" });
					frm.reload_doc();
				});
			});
		}
		if (frm.doc.docstatus === 1 && frm.doc.status === "Success" && !frm.doc.payment_entry) {
			frm.add_custom_button(__("Create Payment Entry"), () => {
				frm.call({ method: "make_payment_entry", doc: frm.doc, freeze: true }).then(() =>
					frm.reload_doc()
				);
			});
		}
	},

	payu_transaction(frm) {
		if (!frm.doc.payu_transaction) return;
		frappe.db
			.get_value("PayU Transaction", frm.doc.payu_transaction, ["amount", "refunded_amount"])
			.then(({ message }) => {
				const refundable = flt(message.amount) - flt(message.refunded_amount);
				frm.set_value("refundable_amount", refundable);
				if (!frm.doc.amount) frm.set_value("amount", refundable);
			});
	},
});
