### PayU Payments for ERPNext

Technical name: `ki_erp_payu` · ERPNext v15 / v16 · by kalvyai

Customers pay Payment Requests (from Sales Orders, Sales Invoices or the Webshop cart) on the PayU
India hosted checkout: UPI, cards, net banking, wallets, EMI and pay later. The Payment Entry is
created automatically, and payments can be refunded, in full or in part, from ERPNext.

### Features

| | |
|---|---|
| **PayU Settings** | Merchant Key, Salt, Sandbox / Production, Test Connection. Saving creates the *PayU* Payment Gateway and Payment Gateway Account. |
| **Hosted checkout** | The payment link posts a signed form to PayU. Every opening of the link is a new **PayU Transaction** with its own txnid (PayU refuses a txnid twice), so emailed links never expire and failed payments can be retried. |
| **Verified result** | The status is always read from PayU's `verify_payment` API, never from the browser or the webhook body. The amount is checked and only INR is accepted. |
| **Hash-checked return and webhook** | SHA-512 response hash checked, including PayU's convenience-fee variant (`additional_charges`). The webhook catches payments completed after the customer closed the browser. |
| **Detailed status** | Cancelled vs failed (`unmappedstatus`), pending, authorised-only. PayU ID, payment method, bank reference, amount debited and convenience fee are kept on the transaction. |
| **Paid twice** | When a customer pays the same link twice (e.g. a late UPI confirmation of an earlier attempt), only one Payment Entry is made and the second payment is flagged for refund. |
| **Status synchronization** | *Check Status with PayU* on the Payment Request and the transaction, and a check every 15 minutes of unfinished payments (10 minutes to 3 days old) and queued refunds. |
| **Refunds** | **PayU Refund**: full or partial, from the Payment Request (*PayU → Refund*), the transaction or the list. Submitting sends it to PayU; it stays *Queued* until PayU reports *Success* or *Failed*. PayU's refusals are explained (e.g. code 214: same amount refunded less than 5 minutes ago). |
| **Refund accounting** | A successful refund creates a *Pay* Payment Entry from the PayU account to the customer, allocated to a credit note when one is given. If it can't be created (e.g. closed period), the refund says why and *Create Payment Entry* retries. |
| **Dashboard refunds** | Refunds made in the PayU dashboard are imported as PayU Refunds and accounted for the same way. |
| **Refund webhook** | Used as a trigger only (PayU doesn't sign it): the refund status is read again from the API. |
| **PayU Notification log** | Everything PayU sends, redacted (no personal or card data, no hashes). Duplicates ignored, invalid hashes flagged as *Rejected*. |
| **Webshop Pay button** | On the customer's order page, *Pay* opens the PayU checkout, even when the Webshop checkout uses another gateway (e.g. Cash on Delivery). Only the customer's own orders can be paid this way. Can be turned off in PayU Settings. |

### Requirements

- ERPNext with the `payments` app.
- A PayU India merchant account (test or live Merchant Key and Salt).

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch version-16
bench install-app ki_erp_payu
```

### Setup

1. Open **PayU Settings**, enter the Merchant Key and Salt (PayU dashboard → Developers → API
   Keys; switch the dashboard to test mode for the Sandbox keys), choose the environment and save.
2. In the PayU dashboard (Developers → Webhooks), add the **Payment Webhook URL** for the
   *Successful* and *Failed* payment events, and the **Refund Webhook URL** for the *Refund* event.
   PayU must reach your site over HTTPS (use a tunnel for testing).
3. Create a Payment Request with the *PayU - INR* Payment Gateway Account. Webshop customers get
   the PayU checkout from the *Pay* button of their orders.

### Refunds

1. From a paid Payment Request: **PayU → Refund** (or from the PayU Transaction). The amount
   defaults to what can still be refunded; optionally pick the customer's credit note.
2. **Submit**: the refund is sent to PayU and becomes *Queued*.
3. Once PayU reports it (webhook, 15-minute check or *Check Status with PayU*), the refund becomes
   *Success* with its Payment Entry, or *Failed* with PayU's message. A failed refund can be
   cancelled; its amount can be refunded again.

### Limitations

- One PayU account per site; INR only (PayU India).
- PayU needs a mobile number and an email for every payment: they're taken from the Payment
  Request, the contact on the order or invoice, or the Customer; set a fallback mobile in PayU
  Settings.
- Pre-authorisation (capture / release from ERPNext) is not supported: a payment PayU only
  authorised is shown as *Authorized* and must be captured or released in the PayU dashboard.
- When another installed app also overrides the Webshop Pay button (e.g. `ki_erp_cashfree`), the
  app installed last handles it. PayU hands the button on to the other app when *Use PayU for the
  Webshop Pay Button* is off.

### Contributing

This app uses `pre-commit` (ruff, eslint, prettier, pyupgrade). Tests never contact PayU: every API
call is replaced by a fake, and a guard fails the test if a real request is attempted.

```bash
bench --site <site> set-config allow_tests true
bench --site <site> run-tests --app ki_erp_payu
```

### License

Proprietary: KalvyAI Software Licence Agreement. See `license.txt`.
