# Bringo Routes Optimizer V2

Bringo Routes Optimizer V2 is a Telegram bot for route planning and store operations.

It keeps the original planning features and adds a persistent admin system directly inside Telegram through `/settings`.

## Main Features

- Route optimization by store, date, slot, and driver count
- Order checks: payment, detailed status, and end-to-end
- Store dashboards
- Admin-only `/settings` menu inside Telegram
- Persistent settings for stores, slots, Redash config, rules, and SLA thresholds

## Admin Commands

- `/start`
- `/settings`
- `/whoami`

`/whoami` helps you get your Telegram user id so you can add it to `ADMIN_USER_IDS`.

## Environment Variables

- `TELEGRAM_TOKEN`: Telegram bot token
- `REDASH_RESULTS_CSV_URL`: Redash CSV export URL
- `REDASH_API_KEY`: optional Redash API key
- `ADMIN_USER_IDS`: comma-separated Telegram user ids allowed to access `/settings`
- `FLASK_SECRET_KEY`: Flask session secret
- `APP_DB_PATH`: SQLite database path
- `WEBHOOK_URL`: optional explicit webhook URL
- `RENDER_EXTERNAL_URL`: automatically provided by Render, used when `WEBHOOK_URL` is not set

## Local Run

```powershell
python -m flask --app bot:flask_app run --host 127.0.0.1 --port 5010
```

## Render Deployment

This project is ready for a Render Web Service.

Recommended setup:

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn bot:flask_app --bind 0.0.0.0:$PORT --workers 1 --threads 8`
- Persistent disk mounted at `/var/data`
- `APP_DB_PATH=/var/data/bringo_admin.db`

Because the bot uses a Flask webhook bridge, Render's public URL can be used automatically via `RENDER_EXTERNAL_URL`, so you usually do not need to set `WEBHOOK_URL` manually.

## Telegram Admin Flow

When an admin sends `/settings`, the bot opens sections for:

- Stores
- Slots
- Redash
- Business rules
- SLA
- Info

Changes made there are global and affect all bot users.

## Security Note

Do not commit real secrets to Git.
Use `.env.example` as a template and configure real values in Render environment variables.
