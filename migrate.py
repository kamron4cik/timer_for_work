"""
migrate.py — Migrate legacy user_data.json → SQLite

Run once automatically on first startup if workbot.db doesn't exist yet.
Safe to run multiple times (idempotent).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import LEGACY_DATA_FILE, DEFAULT_TIMEZONE
from db import get_db, get_or_create_user, WorkSession, Break, init_db

logger = logging.getLogger(__name__)


def run_migration():
    legacy_path = Path(LEGACY_DATA_FILE)
    if not legacy_path.exists():
        logger.info("No legacy user_data.json found — skipping migration.")
        return

    try:
        with open(legacy_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning("Could not read legacy data: %s", e)
        return

    tz = ZoneInfo(DEFAULT_TIMEZONE)

    def parse_dt(s):
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(ZoneInfo("UTC"))

    goals     = raw.get("goals", {})
    sessions  = raw.get("sessions", {})
    history   = raw.get("history", {})

    db = get_db()

    migrated_users    = 0
    migrated_sessions = 0

    for uid_str, goal in goals.items():
        try:
            uid = int(uid_str)
        except ValueError:
            continue

        user = get_or_create_user(db, uid)
        user.goal_hours = float(goal)
        db.commit()
        migrated_users += 1

        # Migrate active session
        sess_data = sessions.get(uid_str)
        if sess_data and sess_data.get("start_time"):
            try:
                started_at = parse_dt(sess_data["start_time"])
                status_map = {
                    "working": "active",
                    "paused":  "paused",
                    "idle":    None,
                    "done":    "done",
                }
                status = status_map.get(sess_data.get("status", "idle"))
                if status and started_at:
                    sess_date = started_at.astimezone(tz).date()
                    ws = WorkSession(
                        user_id=user.id,
                        date=sess_date,
                        started_at=started_at,
                        ended_at=None,
                        status=status,
                        session_type="normal",
                        chat_id=sess_data.get("chat_id"),
                    )
                    db.add(ws)
                    db.flush()

                    # Migrate timeline segments as breaks
                    for seg in sess_data.get("timeline", []):
                        if seg.get("type") == "break":
                            b = Break(
                                session_id=ws.id,
                                started_at=parse_dt(seg["start"]) or started_at,
                                ended_at=parse_dt(seg.get("end")),
                                break_type="manual",
                            )
                            db.add(b)

                    db.commit()
                    migrated_sessions += 1
            except Exception as e:
                logger.warning("Error migrating session for user %s: %s", uid_str, e)
                db.rollback()

        # Migrate history as completed sessions
        user_hist = history.get(uid_str, {})
        for date_str, entry in user_hist.items():
            try:
                from datetime import date as date_cls, time as time_cls
                d = date_cls.fromisoformat(date_str)
                worked_secs = float(entry.get("worked", 0))

                if worked_secs <= 0:
                    continue

                # Reconstruct approximate start/end times
                day_start = datetime.combine(d, time_cls(9, 0), tzinfo=tz)
                day_start_utc = day_start.astimezone(ZoneInfo("UTC"))

                # Check if already migrated
                from db import get_sessions_for_range
                existing = get_sessions_for_range(db, user.id, d, d)
                if existing:
                    continue   # Already handled above

                import datetime as dt_module
                ended_utc = day_start_utc + dt_module.timedelta(seconds=worked_secs + 3600)  # +1h lunch approx

                ws = WorkSession(
                    user_id=user.id,
                    date=d,
                    started_at=day_start_utc,
                    ended_at=ended_utc,
                    status="done",
                    session_type="normal",
                )
                db.add(ws)
                db.commit()
                migrated_sessions += 1
            except Exception as e:
                logger.warning("Error migrating history %s for user %s: %s", date_str, uid_str, e)
                db.rollback()

    db.close()

    logger.info(
        "Migration complete: %d users, %d sessions.",
        migrated_users, migrated_sessions,
    )
    print(f"✅ Migration complete: {migrated_users} user(s), {migrated_sessions} session(s) imported.")


if __name__ == "__main__":
    init_db()
    run_migration()

