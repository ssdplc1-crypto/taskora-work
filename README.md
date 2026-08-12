# TASKORA WORK MVP

**Learn • Work • Earn**

A mobile-first Flask/PWA MVP for a Nigerian work marketplace.

## Core rules

- Registration is free.
- Worker activation fee: **₦3,000**.
- Activation fee is **not** an earning balance and is not an investment/deposit.
- Earnings come only from approved tasks.
- Minimum withdrawal: **₦5,000**.
- Withdrawal requests open on **Friday**.
- Admin reviews/processes withdrawals.
- Flutterwave is integrated through server-side API calls.
- Real credentials must be supplied through environment variables.

## Important before going live

This project is production-oriented but it is still an MVP. Before collecting money from the public:

1. Complete Flutterwave live-business/merchant onboarding using your real information.
2. Confirm that Flutterwave approves this exact marketplace/activation-fee business model.
3. Configure a public HTTPS `BASE_URL`.
4. Set the Flutterwave webhook URL to:
   `https://YOUR-DOMAIN/webhooks/flutterwave`
5. Set the webhook verification hash in `FLW_WEBHOOK_HASH`.
6. Set a strong `FLASK_SECRET_KEY` and `ADMIN_PASSWORD`.
7. Never commit `.env` or live API keys to GitHub.
8. Review Nigerian legal, tax, consumer-protection, privacy and payment requirements before public launch.
9. Use a proper production database/backups before scaling. SQLite is convenient for MVP/development; PostgreSQL is recommended for a larger deployment.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

On Windows:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python app.py
```

Open `http://localhost:5000`.

Default admin email is `admin@taskora.local`. Set `ADMIN_PASSWORD` before first run. Change it before deployment.

## Flutterwave flow

### Activation
1. User clicks Activate.
2. Server creates a Flutterwave payment.
3. User is redirected to Flutterwave.
4. Callback verifies the transaction server-side.
5. User is activated only if amount, currency and transaction reference match.

### Withdrawals
1. User must have an available balance of at least ₦5,000.
2. User submits a Friday withdrawal request.
3. Admin reviews it.
4. Server creates a Flutterwave transfer using the verified bank details.
5. Provider/webhook status should be used to confirm final outcome.

Do not mark a withdrawal as paid merely because a transfer request was accepted.

## MVP limitations to address before scale

- Replace SQLite with PostgreSQL.
- Add bank-account name enquiry/verification through the chosen provider.
- Add rate limiting and CSRF protection middleware.
- Add stronger KYC/identity verification.
- Add automated transfer webhook reconciliation.
- Add task/business escrow and business accounts.
- Add audit logs.
- Add background job queue for payout reconciliation.
- Add privacy policy, terms, dispute/refund workflow.
- Add automated monitoring and backups.

## Project structure

```text
taskora-work-mvp/
├── app.py
├── requirements.txt
├── .env.example
├── Procfile
├── render.yaml
├── README.md
├── templates/
└── static/
```
