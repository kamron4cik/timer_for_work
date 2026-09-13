"""
config.py — WorkBot Configuration Defaults

Edit these constants to change the default schedule.
Individual users can override via /schedule and /settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ───────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# ── Timezone ───────────────────────────────────────────────────
DEFAULT_TIMEZONE: str = os.getenv("TIMEZONE", "Asia/Tashkent")

# ── Default Work Schedule ──────────────────────────────────────
# Weekdays: 0=Monday … 6=Sunday
DEFAULT_WORK_DAYS: list[int] = [0, 1, 2, 3, 4]   # Mon–Fri

DEFAULT_WORK_START: str = "09:00"   # HH:MM in local timezone
DEFAULT_WORK_END:   str = "18:00"   # HH:MM in local timezone

# Required net working hours (excluding breaks)
DEFAULT_REQUIRED_HOURS: float = 8.0

# ── Mandatory Lunch Break ──────────────────────────────────────
DEFAULT_LUNCH_START: str = "13:00"
DEFAULT_LUNCH_END:   str = "14:00"

# ── Scheduler Intervals ────────────────────────────────────────
# How often (seconds) the safety-net progress job runs.
# Dynamic precise jobs are also scheduled for each key event.
SAFETY_NET_INTERVAL: int = 60   # 1 minute safety-net poll

# ── Database ───────────────────────────────────────────────────
import os as _os
DB_PATH: str = _os.path.join(_os.path.dirname(__file__), "workbot.db")
DATABASE_URL: str = f"sqlite:///{DB_PATH}"

# ── Legacy migration ───────────────────────────────────────────
LEGACY_DATA_FILE: str = _os.path.join(_os.path.dirname(__file__), "user_data.json")
