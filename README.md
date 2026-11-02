# TIKETI360 Kenya — Talanta Stadium ticketing MVP

A Flask + SQLite ticket-booking website with customer details, event management, demo checkout, M-PESA Daraja STK Push integration scaffold, QR-code digital tickets, PDF downloads, and an admin ticket scanner.

## Important launch status
- The included Talanta Stadium event is explicitly a **demo event**, not a real fixture or an authorised event listing.
- The application starts in `PAYMENT_MODE=DEMO`. Demo checkout simulates payment and must never be presented as a real M-PESA transaction.
- Live M-PESA requires your own Safaricom Daraja app, approved credentials, a public HTTPS callback URL, and testing. Do not take live customer money until you have the event organiser's ticketing authorisation and have completed payment reconciliation tests.
- Change the admin password and secret key before exposing the site to a network.

## Run on Parrot OS / Linux

```bash
cd tiketi360
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open:
- Public website: http://127.0.0.1:5000
- Admin: http://127.0.0.1:5000/admin/login
- Default local demo admin password: `ChangeThisAdminPassword!` (change it in `.env` immediately)

## Demo checkout
1. Open the demo event.
2. Enter customer name, email and phone.
3. Choose Regular (KES 1,500) or VIP (KES 5,000), and quantity (1–6).
4. Click continue to payment.
5. In DEMO mode, click **Simulate demo payment**.
6. A paid sample ticket with a unique QR code is generated. Download its PDF.
7. Log into the admin dashboard and use the scanner to test first scan approval and duplicate scan rejection.

The sample prices are editable demonstration values, not official Talanta Stadium prices.

## M-PESA Daraja sandbox
1. Create/configure a Daraja app in Safaricom Developer Portal.
2. Copy `.env.example` to `.env` and enter sandbox consumer key, consumer secret, shortcode and passkey.
3. Expose your local app through a public HTTPS tunnel for sandbox callback testing, then set `MPESA_CALLBACK_URL` to `https://YOUR-HOST/api/mpesa/callback`.
4. Set `BASE_URL` to the same public HTTPS origin.
5. Set `PAYMENT_MODE=MPESA` and restart the app.
6. Test only with Daraja sandbox credentials. The callback changes a booking to PAID only when Safaricom returns `ResultCode=0`.

This is an integration starter, not a production payment certification. Before production, add callback signature/source validation as supported by the provider, idempotency/reconciliation jobs, rate limiting, CSRF protection, HTTPS, secure secrets management, database migrations, monitoring, backups, privacy notice and a tested refund workflow.

## Routes
- `/` — event listing
- `/book/<slug>` — customer booking form
- `/payment/<reference>` — checkout
- `/ticket/<reference>` — digital ticket
- `/ticket/<reference>/pdf` — PDF ticket
- `/verify/<token>` — public ticket status
- `/admin/login` and `/admin` — admin dashboard
- `/health` — health check

## Before government or public sale
Obtain written permission from the stadium/event rights holder and the event organiser to list the event, allocate inventory and issue tickets. Confirm applicable procurement, data-protection, consumer-protection, tax, payment-provider and event-security requirements. The app is a functional MVP for demonstration and further hardening, not a certified government production platform.
