# paper-trail

A small web app that matches receipts in Paperless-ngx to transactions in an Actual Budget export, and shows which transactions have no receipt. Built for collecting records for a tax preparer.

## How it works

- You export transactions from Actual as CSV and upload the file on the page.
- The app reads receipts from the Paperless API, filtered by tag, and matches each transaction to a receipt by amount, date and vendor name.
- The amount comes from a Paperless custom field (default `amount`). If that is empty, it looks for the amount in the receipt's OCR text.
- It lists transactions with no receipt, and receipts with no transaction, per business.
- Once a week it can email the missing list, using the last export you uploaded.

Transfers between accounts, income and starting balances are skipped.

## Setup

1. Copy `docker-compose.example.yml`, edit the values, and set `PAPERLESS_TOKEN` (Paperless, My Profile, API Auth Token).
2. `docker compose up -d --build`
3. Open port 8080 behind your own authentication.

## Settings

| Variable | Meaning |
| --- | --- |
| `PAPERLESS_URL` | Base URL of Paperless |
| `PAPERLESS_TOKEN` | Paperless API token |
| `BUSINESSES` | `tag\|Actual account name`, separated by `;` |
| `AMOUNT_FIELD` | Paperless custom field holding the amount, default `amount` |
| `WEEKLY_EMAIL` | `mon 08:00` style day and time, or `off` |
| `SMTP_HOST`, `SMTP_PORT` | Default `smtp.fastmail.com`, `465` |
| `SMTP_USER`, `SMTP_PASS`, `MAIL_TO` | Needed for email |
| `DATA_DIR` | Where uploads and results are kept, default `/data` |

## Limits

- Actual has no unattended export. If your Actual server only allows OpenID login, the API cannot sign in with a password, so the app works from the CSV you upload.
- The page has no login of its own and handles bank data. Run it behind SSO or on a private network only.
- Matching is a best guess. Tips, split payments and dates that differ by more than a week can miss.

## Command line

`match_receipts.py` also works on its own:

    PAPERLESS_TOKEN=... python3 match_receipts.py --actual export.csv --year 2026 --tag my-tag --account "My Account" --outdir out
