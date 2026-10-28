frappe.ui.form.on("Payment Request", {
	refresh(frm) {
		if (frm.doc.payment_gateway !== "PayU" || frm.doc.docstatus !== 1) return;

		if (frm.doc.status !== "Cancelled") {
			frm.add_custom_button(__("Check Status with PayU"), () => {
				frappe
					.call({
						method: "ki_erp_payu.api.check_payment_status",
						args: { payment_request: frm.doc.name },
						freeze: true,
					})
					.then((r) => {
						frappe.show_alert({ message: r.message, indicator: "blue" });
						frm.reload_doc();
					});
			}, __("PayU"));
		}

		frm.add_custom_button(__("Transactions"), () => {
			frappe.set_route("List", "PayU Transaction", { reference_docname: frm.doc.name });
		}, __("PayU"));

		if (frm.doc.status === "Paid" && frappe.model.can_create("PayU Refund")) {
			frm.add_custom_button(__("Refund"), () => {
				frappe
					.call({
						method: "ki_erp_payu.api.get_refundable_transactions",
						args: { payment_request: frm.doc.name },
					})
					.then(({ message }) => {
						const tx = (message || []).find((t) => flt(t.amount) > flt(t.refunded_amount));
						if (!tx) {
							frappe.msgprint(__("Nothing left to refund on the PayU payments of {0}.", [frm.doc.name]));
							return;
						}
						frappe.new_doc("PayU Refund", {
							payu_transaction: tx.name,
							amount: flt(tx.amount) - flt(tx.refunded_amount),
						});
					});
			}, __("PayU"));
		}
	},
});
