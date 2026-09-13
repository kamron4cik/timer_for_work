# WorkBot — Professional Telegram Work-Time Tracker

A professional Telegram bot for tracking and calculating working hours with full automation, accuracy, and reliability.

## Features

- **Accurate event-based timer** — never drifts; calculated from stored timestamps, not counters
- **Auto lunch-break** — pauses at 13:00, resumes at 14:00 automatically
- **Dynamic completion notification** — fires at the *exact* projected done time based on your actual start
- **Non-working day detection** — Saturday/Sunday are off; "Start Anyway" for extra work
- **Holiday support** — mark specific dates as non-working
- **Manual breaks** — pause/resume any time; each break stored separately
- **Historical corrections** — edit start/end time, add breaks, delete sessions
- **CSV & Excel export** — today / this week / this month with styled `.xlsx`
- **Weekly & monthly reports** — detailed breakdown with progress bars
- **Session recovery** — bot restores all jobs after a restart
- **Configurable schedule** — per-weekday work hours and required hours

## Setup

```bash
# Install dependencies
pip3 install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env and set your BOT_TOKEN
# Optionally set TIMEZONE (default: Asia/Tashkent)

# Run
python3 bot.py
```

## File Structure

| File | Purpose |
|------|---------|
| `bot.py` | Telegram handlers, keyboards, message formatting |
| `db.py` | SQLAlchemy ORM models + query helpers (SQLite) |
| `calculator.py` | Pure deterministic working-time engine |
| `scheduler.py` | Background job logic (reminders, lunch, done notification) |
| `exporter.py` | CSV + Excel export |
| `config.py` | Default schedule constants |
| `migrate.py` | One-time migration from legacy `user_data.json` |
| `workbot.db` | SQLite database (auto-created on first run) |

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message + current status |
| `/begin` | Start work session |
| `/pause` | Start a manual break |
| `/resume` | Resume from break |
| `/status` | Live status card with timer |
| `/done` or `/stop` | Finish session early |
| `/report` | Weekly/monthly work reports |
| `/export` | Export to CSV or Excel |
| `/holiday` | Manage holidays & non-working days |
| `/edit` | Correct session times or add breaks |
| `/setgoal` | Set daily work goal (hours) |
| `/reset` | Reset today's session |

## Default Schedule

Configured in `config.py`:
- **Working days:** Monday–Friday
- **Hours:** 09:00–18:00
- **Mandatory lunch:** 13:00–14:00
- **Required work:** 8 hours/day
- **Timezone:** Asia/Tashkent (UTC+5)

## Database Schema

Tables: `users`, `work_schedules`, `break_schedules`, `work_sessions`, `breaks`, `holidays`, `notification_logs`

All timestamps stored as UTC. All display is done in the user's configured timezone.
