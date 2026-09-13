"""
scheduler.py — WorkBot Background Job Scheduling

Handles:
  • Morning reminders (if no session started by work_start_time)
  • Auto lunch-break start
  • Auto lunch-break resume
  • Dynamic work-completion notification (fires at projected_done_at)
  • End-of-day safety reminder

Two-tier design:
  1. Daily setup job  — runs at midnight and on bot startup to schedule today's events.
  2. Dynamic done job — created/updated whenever a session starts or a break changes.

All jobs are idempotent via notification_logs table.
"""

from __future__ import annotations

import logging
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from telegram.ext import ContextTypes, Application

from db import (
    get_db, get_or_create_user, get_active_session, get_open_break,
    get_schedule_for_weekday, is_holiday,
    notification_sent_today, mark_notification_sent,
    get_all_active_sessions, Break, WorkSession,
)
from calculator import calculate, BreakPeriod, fmt_dur, fmt_t, pbar
from config import SAFETY_NET_INTERVAL

logger = logging.getLogger(__name__)


# ── Job name helpers ───────────────────────────────────────────

def _job_name(kind: str, uid: int) -> str:
    return f"wb_{kind}_{uid}"


def _remove_jobs(app: Application, name: str):
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()


# ── Schedule daily events for a single user ────────────────────

def schedule_daily_jobs(app: Application, user_id: int, chat_id: int, tz: ZoneInfo):
    """
    Schedule today's time-based events for a user.
    Called at bot startup and at midnight each day.
    """
    db = get_db()
    try:
        now_local = datetime.now(tz=tz)
        today = now_local.date()
        weekday = today.weekday()

        sched = get_schedule_for_weekday(db, user_id, weekday)
        holiday = is_holiday(db, user_id, today)

        if not sched or not sched.is_working_day or holiday:
            return  # Nothing to schedule on non-working / holiday days

        work_start: time = sched.start_time or time(9, 0)
        work_end:   time = sched.end_time   or time(18, 0)

        # ── Morning reminder ──
        reminder_dt = datetime.combine(today, work_start, tzinfo=tz)
        if reminder_dt > now_local:
            _remove_jobs(app, _job_name("morning", user_id))
            app.job_queue.run_once(
                _morning_reminder_job,
                when=reminder_dt,
                name=_job_name("morning", user_id),
                data={"user_id": user_id, "chat_id": chat_id, "tz": tz},
            )

        # ── Lunch start / end ──
        # These are handled by the safety-net progress job but we also
        # schedule precise one-shot triggers.
        from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
        ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
        le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))
        lunch_start_dt = datetime.combine(today, time(ls_h, ls_m), tzinfo=tz)
        lunch_end_dt   = datetime.combine(today, time(le_h, le_m), tzinfo=tz)

        if lunch_start_dt > now_local:
            _remove_jobs(app, _job_name("lunch_start", user_id))
            app.job_queue.run_once(
                _lunch_start_job,
                when=lunch_start_dt,
                name=_job_name("lunch_start", user_id),
                data={"user_id": user_id, "chat_id": chat_id, "tz": tz},
            )

        if lunch_end_dt > now_local:
            _remove_jobs(app, _job_name("lunch_end", user_id))
            app.job_queue.run_once(
                _lunch_end_job,
                when=lunch_end_dt,
                name=_job_name("lunch_end", user_id),
                data={"user_id": user_id, "chat_id": chat_id, "tz": tz},
            )

    finally:
        db.close()


def schedule_done_job(app: Application, user_id: int, chat_id: int, tz: ZoneInfo, done_at: datetime):
    """Schedule a one-shot job to fire exactly when work goal is projected to complete."""
    now = datetime.now(tz=tz)
    if done_at <= now:
        return  # Already past — fire immediately
    _remove_jobs(app, _job_name("done", user_id))
    app.job_queue.run_once(
        _goal_reached_job,
        when=done_at,
        name=_job_name("done", user_id),
        data={"user_id": user_id, "chat_id": chat_id, "tz": tz},
    )
    logger.info("Scheduled done_job for user %d at %s", user_id, done_at.strftime("%H:%M"))


def cancel_done_job(app: Application, user_id: int):
    _remove_jobs(app, _job_name("done", user_id))


def start_safety_net(app: Application, user_id: int, chat_id: int):
    """Start the 60-second safety-net poll for this user."""
    name = _job_name("net", user_id)
    _remove_jobs(app, name)
    app.job_queue.run_repeating(
        _safety_net_job,
        interval=SAFETY_NET_INTERVAL,
        first=10,
        name=name,
        data={"user_id": user_id, "chat_id": chat_id},
    )


def stop_safety_net(app: Application, user_id: int):
    _remove_jobs(app, _job_name("net", user_id))


# ── Job implementations ────────────────────────────────────────

async def _morning_reminder_job(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    user_id, chat_id, tz = d["user_id"], d["chat_id"], d["tz"]
    db = get_db()
    try:
        today = datetime.now(tz=tz).date()

        # Idempotency check
        if notification_sent_today(db, user_id, "morning_reminder", today):
            return

        # Check if session already started
        sess = get_active_session(db, user_id, today)
        if sess:
            return  # Already working — no reminder needed

        # Check holiday
        holiday = is_holiday(db, user_id, today)
        if holiday:
            return

        mark_notification_sent(db, user_id, "morning_reminder", today)

        from db import get_schedule_for_weekday as gs
        sched = gs(db, user_id, today.weekday())
        start_str = sched.start_time.strftime("%H:%M") if sched and sched.start_time else "09:00"

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Start Work", callback_data="begin"),
            InlineKeyboardButton("📅 View Schedule", callback_data="view_schedule"),
        ]])
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔔 *Good morning!*\n\n"
                f"Your working day starts at *{start_str}*.\n"
                f"You haven't started your work session yet.\n\n"
                f"_Ready when you are!_ 💪"
            ),
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("morning_reminder_job error for user %d: %s", user_id, e)
    finally:
        db.close()


async def _lunch_start_job(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    user_id, chat_id, tz = d["user_id"], d["chat_id"], d["tz"]
    db = get_db()
    try:
        today = datetime.now(tz=tz).date()
        sess = get_active_session(db, user_id, today)

        if not sess or sess.status != "active":
            return  # Not working

        if notification_sent_today(db, user_id, "lunch_start", today):
            return

        # Auto-pause for lunch
        open_brk = get_open_break(db, sess.id)
        if open_brk:
            return  # Already on a break

        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        brk = Break(session_id=sess.id, started_at=now_utc, ended_at=None, break_type="lunch")
        db.add(brk)
        sess.status = "lunch"
        db.commit()
        mark_notification_sent(db, user_id, "lunch_start", today)

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from config import DEFAULT_LUNCH_END
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("▶️ Work Anyway", callback_data="lunch_override"),
            InlineKeyboardButton("📊 Status", callback_data="status"),
        ], [
            InlineKeyboardButton("🏁 Finish Day", callback_data="done"),
        ]])
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🟡 *LUNCH BREAK*\n\n"
                f"13:00–{DEFAULT_LUNCH_END}\n\n"
                f"Your work timer has been *automatically paused* for lunch.\n"
                f"Enjoy your meal! 🍱\n\n"
                f"_Resuming automatically at {DEFAULT_LUNCH_END}_ ⏰"
            ),
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("lunch_start_job error for user %d: %s", user_id, e)
    finally:
        db.close()


async def _lunch_end_job(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    user_id, chat_id, tz = d["user_id"], d["chat_id"], d["tz"]
    db = get_db()
    try:
        today = datetime.now(tz=tz).date()
        sess = get_active_session(db, user_id, today)

        if not sess or sess.status != "lunch":
            return

        if notification_sent_today(db, user_id, "lunch_end", today):
            return

        # Close lunch break
        open_brk = get_open_break(db, sess.id)
        now_utc = datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))
        if open_brk:
            open_brk.ended_at = now_utc

        sess.status = "active"
        db.commit()
        mark_notification_sent(db, user_id, "lunch_end", today)

        # Recalculate and reschedule done job
        _reschedule_done_job_for(ctx.application, db, sess, user_id, chat_id, tz)

        # Get stats
        from db import get_schedule_for_weekday as gs
        sched = gs(db, user_id, today.weekday())
        from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
        ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
        le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))
        from datetime import time as dtime
        result = _calc_session(sess, sched, tz, dtime(ls_h, ls_m), dtime(le_h, le_m))

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⏸ Break", callback_data="pause"),
            InlineKeyboardButton("📊 Status", callback_data="status"),
        ], [
            InlineKeyboardButton("🏁 Finish Day", callback_data="done"),
        ]])
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🟢 *BACK TO WORK!* ⏰\n\n"
                f"Lunch break ended. Your timer has resumed automatically.\n\n"
                f"⏱ Worked so far: *{fmt_dur(result.worked_secs)}*\n"
                f"⏳ Remaining: *{fmt_dur(result.remaining_secs)}*\n\n"
                f"{pbar(result.pct)}\n\n"
                f"💪 Let's finish strong!"
            ),
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("lunch_end_job error for user %d: %s", user_id, e)
    finally:
        db.close()


async def _goal_reached_job(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    user_id, chat_id, tz = d["user_id"], d["chat_id"], d["tz"]
    db = get_db()
    try:
        today = datetime.now(tz=tz).date()
        sess = get_active_session(db, user_id, today)

        if not sess or sess.status == "done":
            return

        if notification_sent_today(db, user_id, "goal_reached", today):
            return

        # Verify goal is actually reached
        from db import get_schedule_for_weekday as gs
        sched = gs(db, user_id, today.weekday())
        from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
        ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
        le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))
        from datetime import time as dtime
        result = _calc_session(sess, sched, tz, dtime(ls_h, ls_m), dtime(le_h, le_m))

        if not result.is_complete:
            # Not done yet — safety net will reschedule
            return

        mark_notification_sent(db, user_id, "goal_reached", today)

        started_local = sess.started_at.astimezone(tz)
        now_local = datetime.now(tz=tz)

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🏁 Finish Day", callback_data="done"),
            InlineKeyboardButton("📊 Status", callback_data="status"),
        ], [
            InlineKeyboardButton("📈 Reports", callback_data="reports_menu"),
        ]])
        await ctx.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎉 *WORKDAY COMPLETED!*\n\n"
                f"You've completed your required *{fmt_dur(result.required_secs)}* of working time!\n\n"
                f"🕐 Started: *{fmt_t(started_local)}*\n"
                f"✅ Completed: *{fmt_t(now_local)}*\n"
                f"☕ Lunch: *{fmt_dur(result.mandatory_secs)}*\n"
                f"🧘 Manual breaks: *{fmt_dur(result.manual_secs)}*\n"
                f"⏱ Total working time: *{fmt_dur(result.worked_secs)}*\n\n"
                f"{pbar(100)}\n\n"
                f"🌙 *Well done! You've earned your rest!*"
            ),
            parse_mode="Markdown",
            reply_markup=kb,
        )
    except Exception as e:
        logger.error("goal_reached_job error for user %d: %s", user_id, e)
    finally:
        db.close()


async def _safety_net_job(ctx: ContextTypes.DEFAULT_TYPE):
    """
    60-second safety net. Handles edge cases if precise jobs missed their window.
    Also detects goal completion in near-real-time.
    """
    d = ctx.job.data
    user_id, chat_id = d["user_id"], d["chat_id"]
    db = get_db()
    try:
        user = db.query(__import__("db").User).filter_by(id=user_id).first()
        if not user:
            ctx.job.schedule_removal()
            return

        tz = ZoneInfo(user.timezone)
        today = datetime.now(tz=tz).date()
        sess = get_active_session(db, user_id, today)

        if not sess:
            ctx.job.schedule_removal()
            return

        if sess.status == "done":
            ctx.job.schedule_removal()
            return

        from db import get_schedule_for_weekday as gs
        sched = gs(db, user_id, today.weekday())
        from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
        ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
        le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))
        from datetime import time as dtime
        result = _calc_session(sess, sched, tz, dtime(ls_h, ls_m), dtime(le_h, le_m))

        # Check if goal is now complete but notification not yet sent
        if result.is_complete and not notification_sent_today(db, user_id, "goal_reached", today):
            await _goal_reached_job(ctx)

    except Exception as e:
        logger.error("safety_net_job error for user %d: %s", user_id, e)
    finally:
        db.close()


# ── Helper ─────────────────────────────────────────────────────

def _calc_session(sess, sched, tz, lunch_start, lunch_end):
    from calculator import calculate, BreakPeriod
    required = (sched.required_hours if sched and sched.required_hours else 8.0)
    break_periods = [
        BreakPeriod(b.started_at.replace(tzinfo=ZoneInfo("UTC")), b.ended_at.replace(tzinfo=ZoneInfo("UTC")) if b.ended_at else None, b.break_type)
        for b in sess.breaks
    ]
    return calculate(
        session_started_at=sess.started_at.replace(tzinfo=ZoneInfo("UTC")),
        session_ended_at=sess.ended_at.replace(tzinfo=ZoneInfo("UTC")) if sess.ended_at else None,
        breaks=break_periods,
        required_hours=required,
        scheduled_break_start=lunch_start,
        scheduled_break_end=lunch_end,
        tz=tz,
    )


def _reschedule_done_job_for(app, db, sess, user_id, chat_id, tz):
    from db import get_schedule_for_weekday as gs
    from datetime import date as date_cls, time as dtime
    today = datetime.now(tz=tz).date()
    sched = gs(db, user_id, today.weekday())
    from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
    ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
    le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))
    result = _calc_session(sess, sched, tz, dtime(ls_h, ls_m), dtime(le_h, le_m))
    if result.projected_done_at:
        schedule_done_job(app, user_id, chat_id, tz, result.projected_done_at)


# ── Startup restore ────────────────────────────────────────────

async def restore_scheduler_jobs(app: Application):
    """Called on bot startup to restore jobs for all active sessions."""
    db = get_db()
    try:
        active_sessions = get_all_active_sessions(db)
        count = 0
        for sess in active_sessions:
            if not sess.chat_id:
                continue
            user = db.query(__import__("db").User).filter_by(id=sess.user_id).first()
            if not user:
                continue
            tz = ZoneInfo(user.timezone)
            # Restore safety net
            start_safety_net(app, user.telegram_id, sess.chat_id)
            # Restore daily jobs
            schedule_daily_jobs(app, user.id, sess.chat_id, tz)
            # Reschedule done job if session is active
            if sess.status in ("active", "lunch"):
                _reschedule_done_job_for(app, db, sess, user.id, sess.chat_id, tz)
            count += 1
        if count:
            logger.info("Restored scheduler jobs for %d active session(s)", count)
    finally:
        db.close()
