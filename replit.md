# BeachManager

A beach staff management system with a Telegram bot for employees and a web admin panel for the owner.

## Run & Operate

- Admin panel: runs automatically via the `artifacts/beach-admin: web` workflow (port 24230)
- Telegram bot: runs automatically via the `BeachManager Telegram Bot` workflow
- Admin login password: `beach2024` (change via `ADMIN_PASSWORD` env var)

## Stack

- Python 3.11
- Flask (admin web panel)
- python-telegram-bot (Telegram bot)
- SQLite (database at `beach-manager/beach_manager.db`)

## Where things live

- `beach-manager/database.py` — DB schema, init, and seed data
- `beach-manager/admin/app.py` — Flask admin panel (all routes)
- `beach-manager/bot/handlers.py` — Telegram bot command/callback handlers
- `beach-manager/bot/main.py` — Bot entry point
- `beach-manager/templates/` — Jinja2 HTML templates
- `beach-manager/static/css/style.css` — Dark theme CSS

## Architecture decisions

- SQLite is used instead of PostgreSQL for simplicity — no external DB needed, file lives at `beach-manager/beach_manager.db`
- The Telegram bot and Flask admin share the same SQLite database file directly
- Admin panel is a traditional server-rendered Flask app (Jinja2 templates), not a React SPA
- The artifact system is used to register the Flask app with the proxy (artifact type `react-vite` shell, dev command overridden to run Python)
- Notifications are stored in the DB queue; the bot checks them when users interact (pull model, not push)

## Product

- **Admin Panel** (`/`): Dashboard with KPIs, 7-day financial chart, staff overview, task list, notifications. Pages: Staff, Tasks, Finances (income/expenses by date), Reports (weekly charts, top categories, staff performance).
- **Telegram Bot**: Employees use `/start` to register, then can view their tasks, mark tasks done, see stats, and check today's schedule via inline buttons.

## User preferences

- Language: Python
- Stack: Flask + python-telegram-bot + SQLite

## Gotchas

- The `artifacts/beach-admin` directory contains a React-Vite scaffold shell that is NOT used — the dev command in `artifact.toml` runs the Flask app instead
- Do NOT run `pnpm --filter @workspace/beach-admin run dev` — it will fail (no PORT/BASE_PATH for Vite)
- Telegram bot notifications are queue-based (stored in DB); actual push delivery to Telegram requires polling the notifications table and calling bot.send_message — not yet implemented for push
- The DB is seeded with demo data (10 employees, tasks, income, expenses) only if the employees table is empty on first run

## Pointers

- See the `pnpm-workspace` skill for workspace structure
