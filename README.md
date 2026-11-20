# Restaurant Table Booking System (TBS)

SWEN 261, Group 03. A Flask web app that keeps one shared, conflict-checked record of table
bookings for a single small restaurant. Guests book online, staff enter phone and walk-in
bookings, and the system refuses any request that would promise the same table twice.

The code follows our three milestone documents:

- M1 Domain Analysis: the problem and the eight domain entities.
- M2 SRS: the FR, IF, NFR, DR and CON requirements. Comments and test names cite these IDs
  (for example `AC-FR-038.2`).
- M3 Design Description: the layering. `tbs/services/` has one service per Level 0 process,
  `tbs/repositories.py` has one repository per data store (D1 to D7), `tbs/web/` holds the
  boundary classes, and `tbs/services/mail.py` is the `EmailGateway` interface with an SMTP adapter.

## Run it

You need Python 3.11 or newer.

```bash
git clone <your-repo-url>
cd restaurant-table-booking-system
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

export TBS_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
flask --app tbs seed-demo          # sample tables, opening hours, two staff accounts
python run.py                      # http://127.0.0.1:5000
```

`seed-demo` prints a random password for `manager@example.com` and `host@example.com`.
For a real restaurant skip it and use `flask --app tbs create-account` to make the first manager,
then fill in the setup page (tables, service periods, contact phone, time zone).

Pages:

| Who | Address |
| --- | --- |
| Guest booking | `/book` |
| Guest links from e-mails | `/cancel/<token>`, `/change/<token>`, `/attend/<token>` |
| Staff sign-in | `/staff/login` |
| Day list, new booking, walk-ins | `/staff/day`, `/staff/booking/new` |
| Manager only | `/staff/setup`, `/staff/reports`, `/staff/exceptions`, `/staff/accounts`, `/staff/guest-data` |

With the default `console` mail backend nothing is sent. Each message is written to `instance/outbox/`.

## Test it

```bash
python -m pytest
```

The suite covers the acceptance criteria from the SRS, including 20 simultaneous requests for one
table (exactly one wins), the exact cancellation cutoff second, the no-show grace period, retry
and rate limit rules for e-mail, retention, and role permissions.

## Settings (environment variables)

See `.env.example`. The important ones:

- `TBS_SECRET_KEY`: signs sessions and derives guest link tokens. Changing it invalidates old links.
- `TBS_BASE_URL`: used to build links in e-mails.
- `TBS_MAIL_BACKEND`: `console` or `smtp` (then set the `TBS_SMTP_*` values).
- `TBS_FORCE_HTTPS=1`: redirects HTTP to HTTPS and marks cookies secure (IF-005). Use it behind a TLS proxy.
- `TBS_DATABASE`: path of the SQLite file (default `instance/tbs.sqlite3`).

Booking rules such as grace period, cancellation cutoff, dining durations and retention are
not in code. The manager changes them on the setup page (DR-007).

## Deploy

```bash
gunicorn --workers 1 --threads 4 wsgi:app
```

Keep one worker process, because it also runs the background job for reminders, e-mail retries
and the daily removal of expired guest data. If you prefer cron, set `TBS_SCHEDULER=0` and run
`flask --app tbs run-jobs` every minute.

## How conflicts are prevented

1. Every create or change runs in one `BEGIN IMMEDIATE` transaction, so the overlap check and the
   write are a single step (FR-018).
2. A SQLite trigger rejects overlapping active allocations of one table even if the application
   check is bypassed (DR-002).

## Working with GitHub

```bash
git checkout -b feature/short-name
# make changes, then
python -m pytest
git add -A && git commit -m "Describe the change"
git push -u origin feature/short-name
```

Open a pull request into `main`. The CI workflow in `.github/workflows/ci.yml` runs the tests on
Python 3.11, 3.12 and 3.13, and the pull request template asks which requirement IDs you touched.

## Known limits

- One restaurant, one site (M3 scope). SC-10 to SC-17 from M2 are not built.
- The deferred requirements (departure time, bookings by day and party size, report timing,
  swappable e-mail provider) are not built.
- Retention periods and e-mail limits need confirming with the restaurant and the chosen provider (Q-05, DEP-02).
  This project makes no legal compliance claim.
