"""
WorkBot — Professional Telegram Work-Time Tracker
==================================================
A complete rewrite with:
  • SQLite database (SQLAlchemy ORM)
  • Accurate event-based time calculation
  • Configurable weekly schedule + holidays
  • Automatic lunch-break pause/resume
  • Dynamic work-completion notification
  • CSV & Excel export
  • Historical corrections (edit start/end, add/remove breaks)
  • Non-working day detection with "Start Anyway" option
  • Session recovery after restarts
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputFile,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ConversationHandler, filters, ContextTypes,
)

from config import BOT_TOKEN
from db import (
    init_db, get_db, get_or_create_user, get_active_session, get_open_break,
    get_schedule_for_weekday, is_holiday, get_sessions_for_range,
    WorkSession, Break, Holiday, User, WorkSchedule,
)
from calculator import (
    calculate, BreakPeriod, CalcResult,
    fmt_dur, fmt_t, fmt_dt, pbar,
)
from scheduler import (
    schedule_daily_jobs, start_safety_net, stop_safety_net,
    cancel_done_job, schedule_done_job, restore_scheduler_jobs,
    _calc_session, _reschedule_done_job_for,
)
from migrate import run_migration

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)

# ── Conversation states ────────────────────────────────────────
ASK_GOAL = 0
ASK_EDIT_CHOICE = 10
ASK_EDIT_VALUE  = 11
ASK_HOLIDAY_DATE = 20
ASK_HOLIDAY_REASON = 21

# ── Random response pools ──────────────────────────────────────
def r(pool: list[str]) -> str:
    return random.choice(pool)

BEGIN_MSGS = [
    "🟢 *Timer started!*\n\n🕐 Clocked in at *{t}*\n🎯 Today's goal: *{goal}*\n\nI'll cheer you on at 25%, 50%, 75% and send a 🎉 the moment you're done! Let's go! 💪",
    "▶️ *Work mode activated!*\n\n🕐 Started at *{t}*\n🎯 Target: *{goal}* of focused work\n\nYou've got this — I'm tracking every minute! 🏆",
    "🚀 *And we're off!*\n\n🕐 Clock-in: *{t}*\n🎯 Goal for today: *{goal}*\n\nSit back and work — I'll handle the timekeeping! 😎",
    "💼 *Session started!*\n\n🕐 Began at *{t}*\n🎯 Mission: *{goal}* today\n\nGo make it count! I'll ping you at every milestone. 🌟",
]

PAUSE_MSGS = [
    "⏸ *Break started* — you've earned it!\n\n🕐 Paused at *{t}*\n⏱ Worked so far: *{worked}*\n\nRest up, grab a coffee ☕ — hit *Resume* when you're back!",
    "☕ *Enjoy your break!*\n\n🕐 Paused at *{t}*\n⏱ Great progress: *{worked}* done\n\nI'll keep your time safe. See you soon! 😊",
    "🧘 *Taking a breather?*\n\n🕐 Break at *{t}*\n⏱ *{worked}* logged so far — solid work!\n\nStep away, recharge, and come back fresh! 💆",
    "🌿 *Break time!*\n\n🕐 Paused at *{t}*\n⏱ You've clocked *{worked}* — keep it up!\n\nI'm not going anywhere. Hit *▶️ Resume* whenever you're ready. 🙌",
]

RESUME_MSGS = [
    "▶️ *Welcome back!* Let's finish strong 💪\n\n🕐 Resumed at *{t}*\n⏱ Worked: *{worked}* · ⏳ Left: *{left}*\n\n{bar}",
    "🔥 *Back in the zone!*\n\n🕐 Resumed at *{t}*\n⏱ Done: *{worked}* · ⏳ Remaining: *{left}*\n\n{bar}\n\nYou're doing amazing — keep going! 🚀",
    "💼 *Clock's ticking again!*\n\n🕐 Resumed at *{t}*\n⏱ Progress: *{worked}* · ⏳ To go: *{left}*\n\n{bar}\n\nAlmost there — stay focused! 🎯",
    "⚡ *Let's go!*\n\n🕐 Resumed at *{t}*\n⏱ Logged: *{worked}* · ⏳ Left: *{left}*\n\n{bar}",
]

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ── Keyboard builders ──────────────────────────────────────────

def kb_idle():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  Start Working",      callback_data="begin")],
        [InlineKeyboardButton("🎯  Set Daily Goal",     callback_data="setgoal"),
         InlineKeyboardButton("📅  My Schedule",        callback_data="view_schedule")],
        [InlineKeyboardButton("📊  Reports",            callback_data="reports_menu"),
         InlineKeyboardButton("📤  Export History",     callback_data="export_menu")],
        [InlineKeyboardButton("🗓  Holidays & Days Off", callback_data="holiday_menu")],
    ])

def kb_working():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸  Take a Break",       callback_data="pause"),
         InlineKeyboardButton("📊  My Progress",        callback_data="status")],
        [InlineKeyboardButton("🏁  Stop Work",          callback_data="done"),
         InlineKeyboardButton("📤  Export",             callback_data="export_menu")],
        [InlineKeyboardButton("✏️  Edit Session",       callback_data="edit_menu"),
         InlineKeyboardButton("🔄  Reset Day",          callback_data="reset")],
    ])

def kb_paused():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  Resume Working",     callback_data="resume")],
        [InlineKeyboardButton("📊  My Progress",        callback_data="status"),
         InlineKeyboardButton("🏁  Stop Work",          callback_data="done")],
        [InlineKeyboardButton("✏️  Edit Session",       callback_data="edit_menu"),
         InlineKeyboardButton("🔄  Reset Day",          callback_data="reset")],
    ])

def kb_lunch():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🍱  Enjoy lunch! Back at 14:00", callback_data="status")],
        [InlineKeyboardButton("▶️  Skip Lunch & Keep Working", callback_data="lunch_override")],
        [InlineKeyboardButton("🏁  Stop Work for Today",       callback_data="done")],
    ])

def kb_done():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌅  Start a New Session",  callback_data="reset")],
        [InlineKeyboardButton("📊  Today's Summary",      callback_data="status"),
         InlineKeyboardButton("📊  Reports",              callback_data="reports_menu")],
        [InlineKeyboardButton("📤  Export History",       callback_data="export_menu"),
         InlineKeyboardButton("✏️  Correct Session",      callback_data="edit_menu")],
    ])

def kb_nonworking():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀  Work Anyway (Extra Day)", callback_data="begin_extra")],
        [InlineKeyboardButton("📅  View My Schedule",        callback_data="view_schedule"),
         InlineKeyboardButton("📊  Reports",                 callback_data="reports_menu")],
    ])

def kb_reports_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅  This Week",    callback_data="report_week"),
         InlineKeyboardButton("📆  This Month",   callback_data="report_month")],
        [InlineKeyboardButton("📊  Today's Stats", callback_data="status"),
         InlineKeyboardButton("🔙  Back",          callback_data="back_main")],
    ])

def kb_export_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 CSV — Today",    callback_data="export_csv_today"),
         InlineKeyboardButton("📊 Excel — Today",  callback_data="export_xlsx_today")],
        [InlineKeyboardButton("📄 CSV — This Week",  callback_data="export_csv_week"),
         InlineKeyboardButton("📊 Excel — This Week", callback_data="export_xlsx_week")],
        [InlineKeyboardButton("📄 CSV — This Month",  callback_data="export_csv_month"),
         InlineKeyboardButton("📊 Excel — This Month", callback_data="export_xlsx_month")],
        [InlineKeyboardButton("🔙  Back",              callback_data="back_main")],
    ])

def kb_edit_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🕐  Change Start Time",  callback_data="edit_start"),
         InlineKeyboardButton("🕔  Change End Time",    callback_data="edit_end")],
        [InlineKeyboardButton("➕  Add a Break",        callback_data="edit_add_break"),
         InlineKeyboardButton("🗑  Delete Session",     callback_data="edit_delete")],
        [InlineKeyboardButton("🔙  Back",               callback_data="back_main")],
    ])

def kb_holiday_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕  Add Day Off",         callback_data="holiday_add"),
         InlineKeyboardButton("📋  See All Days Off",    callback_data="holiday_list")],
        [InlineKeyboardButton("🔙  Back",                callback_data="back_main")],
    ])

def kb_for_session(sess: Optional[WorkSession]) -> InlineKeyboardMarkup:
    if not sess:
        return kb_idle()
    status = sess.status
    if status == "active":
        return kb_working()
    if status == "paused":
        return kb_paused()
    if status == "lunch":
        return kb_lunch()
    if status == "done":
        return kb_done()
    return kb_idle()


# ── User / session helpers ─────────────────────────────────────

def _user_tz(user: User) -> ZoneInfo:
    return ZoneInfo(user.timezone)


def _now_local(tz: ZoneInfo) -> datetime:
    return datetime.now(tz=tz)


def _now_utc() -> datetime:
    return datetime.utcnow().replace(tzinfo=ZoneInfo("UTC"))


def _to_utc(dt: datetime) -> datetime:
    return dt.astimezone(ZoneInfo("UTC"))


def _to_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def _get_session_result(db, sess: WorkSession, sched: Optional[WorkSchedule], tz: ZoneInfo) -> CalcResult:
    from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
    ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
    le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))
    return _calc_session(sess, sched, tz, time(ls_h, ls_m), time(le_h, le_m))


def _is_working_day_today(db, user: User) -> tuple[bool, Optional[WorkSchedule], Optional[object]]:
    tz = _user_tz(user)
    today = _now_local(tz).date()
    holiday = is_holiday(db, user.id, today)
    sched = get_schedule_for_weekday(db, user.id, today.weekday())
    is_work = sched is not None and sched.is_working_day and holiday is None
    return is_work, sched, holiday


# ── Status card ─────────────────────────────────────────────────

def _status_card(sess: WorkSession, result: CalcResult, tz: ZoneInfo, sched: Optional[WorkSchedule]) -> str:
    from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END

    status = sess.status
    if status == "active":
        status_line = "🟢  *WORKING*"
    elif status == "lunch":
        status_line = "🟡  *LUNCH BREAK*"
    elif status == "paused":
        status_line = "⚪  *ON A BREAK*"
    elif status == "done":
        status_line = "🔴  *FINISHED*"
    else:
        status_line = "⚫  *IDLE*"

    started_local = _to_tz(sess.started_at, tz)

    lines = [
        status_line,
        "",
        f"🕐  Started:      *{fmt_t(started_local)}*",
        f"⏱  Worked:       *{fmt_dur(result.worked_secs)}*",
        f"🎯  Goal:         *{fmt_dur(result.required_secs)}*",
        f"⏳  Remaining:    *{fmt_dur(result.remaining_secs)}*",
    ]

    if result.mandatory_secs > 60:
        lines.append(f"🍱  Lunch break:  *{fmt_dur(result.mandatory_secs)}*")
    if result.manual_secs > 30:
        lines.append(f"☕  Other breaks: *{fmt_dur(result.manual_secs)}*")

    lines += [
        "",
        "─" * 16,
        pbar(result.pct),
        "─" * 16,
    ]

    if status == "lunch":
        lines += [
            "",
            f"🍱  *Lunch break: {DEFAULT_LUNCH_START} – {DEFAULT_LUNCH_END}*",
            f"_Your timer is paused. Resumes automatically at {DEFAULT_LUNCH_END}._ ⏰",
        ]
    elif status == "active" and result.projected_done_at:
        done_local = _to_tz(result.projected_done_at, tz)
        lines += [
            "",
            f"🏁  Est. finish:  *{fmt_t(done_local)}*",
        ]
    elif status == "paused":
        lines += [
            "",
            "_Timer paused. Tap ▶️ Resume whenever you're ready!_",
        ]

    if result.is_complete:
        overtime = result.worked_secs - result.required_secs
        ot_str = f"  _(+{fmt_dur(overtime)} overtime 🌟)_" if overtime > 60 else ""
        lines += ["", f"🎉  *Goal achieved!*{ot_str}"]

    return "\n".join(lines)


# ── Timeline text ──────────────────────────────────────────────

def _timeline_text(sess: WorkSession, tz: ZoneInfo) -> str:
    if not sess.breaks:
        started_local = _to_tz(sess.started_at, tz)
        ended_local   = _to_tz(sess.ended_at, tz) if sess.ended_at else None
        end_str = fmt_t(ended_local) if ended_local else "now ←"
        return f"*📅 Timeline*\n\n💼  {fmt_t(started_local)} → {end_str}"

    lines = ["*📅 Timeline*", ""]
    # Build segments from breaks
    breaks_sorted = sorted(sess.breaks, key=lambda b: b.started_at)
    cursor = _to_tz(sess.started_at, tz)

    for brk in breaks_sorted:
        b_start = _to_tz(brk.started_at, tz)
        b_end   = _to_tz(brk.ended_at, tz) if brk.ended_at else None
        if b_start > cursor:
            dur = (b_start - cursor).total_seconds()
            lines.append(f"💼  {fmt_t(cursor)} → {fmt_t(b_start)}  _({fmt_dur(dur)})_")
        icon = "🍱" if brk.break_type == "lunch" else "☕"
        end_str = fmt_t(b_end) if b_end else "now ←"
        if b_end:
            dur = (b_end - b_start).total_seconds()
            lines.append(f"{icon}  {fmt_t(b_start)} → {end_str}  _({fmt_dur(dur)})_")
        else:
            lines.append(f"{icon}  {fmt_t(b_start)} → now ← (ongoing)")
        cursor = b_end or _now_local(tz)

    session_end = _to_tz(sess.ended_at, tz) if sess.ended_at else None
    if cursor < (session_end or _now_local(tz)):
        end_str = fmt_t(session_end) if session_end else "now ←"
        dur = ((session_end or _now_local(tz)) - cursor).total_seconds()
        lines.append(f"💼  {fmt_t(cursor)} → {end_str}  _({fmt_dur(dur)})_")

    return "\n".join(lines)


# ── Begin session logic ─────────────────────────────────────────

async def _do_begin(
    uid: int, chat_id: int, app: Application,
    reply_fn, extra: bool = False,
):
    db = get_db()
    try:
        user = get_or_create_user(db, uid)
        tz   = _user_tz(user)
        now  = _now_local(tz)
        today = now.date()

        # Check for existing active session
        existing = get_active_session(db, user.id, today)
        if existing and existing.status != "done":
            sched2  = get_schedule_for_weekday(db, user.id, today.weekday())
            result2 = _get_session_result(db, existing, sched2, tz)
            await reply_fn(
                f"🟢 *You're already clocked in!*\n\n"
                f"⏱ Worked so far: *{fmt_dur(result2.worked_secs)}*  ·  ⏳ Left: *{fmt_dur(result2.remaining_secs)}*\n\n"
                f"Use the buttons below to take a break, check your progress, or finish your day.",
                kb_for_session(existing),
            )
            return

        is_work, sched, holiday = _is_working_day_today(db, user)

        if not extra and not is_work:
            if holiday:
                reason = f" \u2014 *{holiday.reason}*" if holiday.reason else ""
                day_label = (
                    f"🗓 *Day off: {today.strftime('%B %d, %Y')}*{reason}\n\n"
                    f"This date is marked as a holiday in your calendar.\n"
                    f"Reminders and work tracking are paused for today."
                )
            else:
                day_label = (
                    f"🛠 *Today is {WEEKDAY_NAMES[today.weekday()]}*\n\n"
                    f"According to your schedule, this is a day off.\n"
                    f"No need to work today — but I understand if you want to anyway! 😉"
                )
            await reply_fn(
                f"{day_label}\n\n"
                f"────────────────────\n"
                f"If you'd like to log extra work today, it will be tracked separately as *Weekend / Extra Work*.",
                kb_nonworking(),
            )
            return

        now_utc = _to_utc(now)

        sess = WorkSession(
            user_id=user.id,
            date=today,
            started_at=now_utc,
            ended_at=None,
            status="active",
            session_type="extra" if extra else "normal",
            chat_id=chat_id,
        )
        db.add(sess)
        db.commit()

        required_hours = (sched.required_hours if sched and sched.required_hours else user.goal_hours)
        goal_secs = required_hours * 3600

        # Check if starting during lunch window
        from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
        ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
        le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))
        in_lunch = time(ls_h, ls_m) <= now.time() < time(le_h, le_m)

        if in_lunch:
            brk = Break(session_id=sess.id, started_at=now_utc, ended_at=None, break_type="lunch")
            db.add(brk)
            sess.status = "lunch"
            db.commit()
            start_safety_net(app, user.telegram_id, chat_id)
            schedule_daily_jobs(app, user.id, chat_id, tz)
            await reply_fn(
                f"🟡 *Clocked in at {fmt_t(now)} — right at lunch time!*\n\n"
                f"🎯 Goal today: *{fmt_dur(goal_secs)}*\n"
                f"🍱 Lunch break runs until *{DEFAULT_LUNCH_END}*\n\n"
                f"Your timer is *paused automatically* — lunch won't count as work. ✔️\n"
                f"I'll resume your clock at *{DEFAULT_LUNCH_END}* automatically. ⏰\n\n"
                f"_Want to work through lunch? Tap the button below._",
                kb_lunch(),
            )
            return

        # Start normally
        start_safety_net(app, user.telegram_id, chat_id)
        schedule_daily_jobs(app, user.id, chat_id, tz)

        # Schedule done job
        result = _get_session_result(db, sess, sched, tz)
        if result.projected_done_at:
            schedule_done_job(app, user.id, chat_id, tz, result.projected_done_at)

        extra_tag = "\n\n🚀 _This session is logged as Extra Work — have a great day!_" if extra else ""
        await reply_fn(
            r(BEGIN_MSGS).format(t=fmt_t(now), goal=fmt_dur(goal_secs)) + extra_tag,
            kb_working(),
        )
    finally:
        db.close()


# ── Command handlers ───────────────────────────────────────────

async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    chat_id = u.effective_chat.id
    db = get_db()
    try:
        user = get_or_create_user(db, uid)
        tz   = _user_tz(user)
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)
        name  = u.effective_user.first_name or "there"

        sched = get_schedule_for_weekday(db, user.id, today.weekday())
        start_str = sched.start_time.strftime("%H:%M") if sched and sched.start_time else "09:00"
        end_str   = sched.end_time.strftime("%H:%M")   if sched and sched.end_time   else "18:00"

        if sess and sess.status != "done":
            # Already in a session — show status directly
            sched2  = get_schedule_for_weekday(db, user.id, today.weekday())
            result  = _get_session_result(db, sess, sched2, tz)
            card    = _status_card(sess, result, tz, sched2)
            await u.message.reply_text(
                f"👋 *Hey {name}!* Here's where you stand today:\n\n" + card,
                parse_mode="Markdown",
                reply_markup=kb_for_session(sess),
            )
            return

        kb = kb_for_session(sess) if sess else kb_idle()
        await u.message.reply_text(
            f"👋 *Hey {name}! Welcome to WorkBot* 🤖\n\n"
            f"I'm your personal work-time tracker. I keep an accurate record of every "
            f"minute you work — and I'll never let a lunch break sneak into your hours.\n\n"
            f"*What I do for you:*\n"
            f"⏱  Track work sessions with *pause & resume*\n"
            f"🍱  *Auto-pause* at 13:00 lunch, *auto-resume* at 14:00\n"
            f"🎯  Alert you the *exact second* your daily goal is hit\n"
            f"📊  *Weekly & monthly* work reports\n"
            f"📤  *Export* to CSV or Excel in one tap\n"
            f"✏️  Correct any mistake (*edit* start, end, or breaks)\n"
            f"🗓  *Holidays & days off* — no nagging on your time off\n\n"
            f"─────────────────────\n"
            f"🎯  Daily goal:  *{user.goal_hours:g}h*"
            f"  ·  🕐  Schedule: *{start_str}–{end_str}*\n"
            f"─────────────────────\n\n"
            f"Tap *▶️ Start Working* when you're ready! 💪",
            parse_mode="Markdown",
            reply_markup=kb,
        )
    finally:
        db.close()


async def cmd_begin(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async def reply(text, kb):
        await u.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    await _do_begin(u.effective_user.id, u.effective_chat.id, ctx.application, reply)


async def cmd_pause(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    db = get_db()
    try:
        user = get_or_create_user(db, uid)
        tz   = _user_tz(user)
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)

        if not sess or sess.status == "done":
            await u.message.reply_text(
                "👀 *No active session.*\n\nStart one first — tap the button below!",
                parse_mode="Markdown", reply_markup=kb_idle(),
            )
            return
        if sess.status in ("paused", "lunch"):
            await u.message.reply_text(
                "⏸ *You're already on a break!*\n\nTap *▶️ Resume Working* when you're back. ☕",
                parse_mode="Markdown", reply_markup=kb_for_session(sess),
            )
            return
        if sess.status != "active":
            await u.message.reply_text(
                "🤔 Nothing to pause right now.",
                parse_mode="Markdown", reply_markup=kb_for_session(sess),
            )
            return

        now_utc = _now_utc()
        brk = Break(session_id=sess.id, started_at=now_utc, break_type="manual")
        db.add(brk)
        sess.status = "paused"
        db.commit()

        cancel_done_job(ctx.application, user.id)
        sched = get_schedule_for_weekday(db, user.id, today.weekday())
        result = _get_session_result(db, sess, sched, tz)
        await u.message.reply_text(
            r(PAUSE_MSGS).format(t=fmt_t(_now_local(tz)), worked=fmt_dur(result.worked_secs)),
            parse_mode="Markdown",
            reply_markup=kb_paused(),
        )
    finally:
        db.close()


async def cmd_resume(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    db = get_db()
    try:
        user = get_or_create_user(db, uid)
        tz   = _user_tz(user)
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)

        if not sess or sess.status == "done":
            await u.message.reply_text(
                "👀 *No active session.*\n\nStart one first — tap below!",
                parse_mode="Markdown", reply_markup=kb_idle(),
            )
            return
        if sess.status == "active":
            await u.message.reply_text(
                "💼 *You're already working!*\n\nNeed a break? Tap *⏸ Take a Break*.",
                parse_mode="Markdown", reply_markup=kb_working(),
            )
            return
        if sess.status not in ("paused", "lunch"):
            await u.message.reply_text(
                "🤔 Nothing to resume right now.",
                parse_mode="Markdown", reply_markup=kb_for_session(sess),
            )
            return

        # Close open break
        open_brk = get_open_break(db, sess.id)
        if open_brk:
            open_brk.ended_at = _now_utc()
        sess.status = "active"
        db.commit()

        sched = get_schedule_for_weekday(db, user.id, today.weekday())
        result = _get_session_result(db, sess, sched, tz)
        if result.projected_done_at:
            schedule_done_job(ctx.application, user.id, u.effective_chat.id, tz, result.projected_done_at)

        await u.message.reply_text(
            r(RESUME_MSGS).format(
                t=fmt_t(_now_local(tz)),
                worked=fmt_dur(result.worked_secs),
                left=fmt_dur(result.remaining_secs),
                bar=pbar(result.pct),
            ),
            parse_mode="Markdown",
            reply_markup=kb_working(),
        )
    finally:
        db.close()


async def cmd_status(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    db = get_db()
    try:
        user  = get_or_create_user(db, uid)
        tz    = _user_tz(user)
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)

        if not sess:
            await u.message.reply_text(
                "👋 *No session started today.*\n\nHit *▶️ Start Working* whenever you're ready!",
                parse_mode="Markdown", reply_markup=kb_idle(),
            )
            return

        sched  = get_schedule_for_weekday(db, user.id, today.weekday())
        result = _get_session_result(db, sess, sched, tz)
        card   = _status_card(sess, result, tz, sched)
        tl     = _timeline_text(sess, tz)
        await u.message.reply_text(
            card + "\n\n" + tl,
            parse_mode="Markdown",
            reply_markup=kb_for_session(sess),
        )
    finally:
        db.close()


async def cmd_done(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    db = get_db()
    try:
        user  = get_or_create_user(db, uid)
        tz    = _user_tz(user)
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)

        if not sess:
            await u.message.reply_text(
                "👋 *No active session to stop.*\n\nStart one below!",
                parse_mode="Markdown", reply_markup=kb_idle(),
            )
            return

        now_utc = _now_utc()
        open_brk = get_open_break(db, sess.id)
        if open_brk:
            open_brk.ended_at = now_utc
        sess.status = "done"
        sess.ended_at = now_utc
        db.commit()

        stop_safety_net(ctx.application, user.id)
        cancel_done_job(ctx.application, user.id)

        sched  = get_schedule_for_weekday(db, user.id, today.weekday())
        result = _get_session_result(db, sess, sched, tz)
        card   = _status_card(sess, result, tz, sched)
        tl     = _timeline_text(sess, tz)

        if result.is_complete:
            overtime = result.worked_secs - result.required_secs
            ot_str = f"\n🌟 Overtime: *{fmt_dur(overtime)}* — above and beyond!" if overtime > 60 else ""
            congrats = (
                f"🎉 *Day complete! Outstanding work!*\n"
                f"You hit your *{fmt_dur(result.required_secs)}* goal.{ot_str}\n\n"
            )
        else:
            congrats = "🏁 *Session closed.*\n\nEvery minute counts. See you next time! 💙\n\n"
        await u.message.reply_text(
            congrats + card + "\n\n" + tl,
            parse_mode="Markdown",
            reply_markup=kb_done(),
        )
    finally:
        db.close()


async def cmd_reset(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    db = get_db()
    try:
        user  = get_or_create_user(db, uid)
        tz    = _user_tz(user)
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)

        stop_safety_net(ctx.application, user.id)
        cancel_done_job(ctx.application, user.id)

        if sess:
            now_utc = _now_utc()
            open_brk = get_open_break(db, sess.id)
            if open_brk:
                open_brk.ended_at = now_utc
            sess.status = "done"
            if not sess.ended_at:
                sess.ended_at = now_utc
            db.commit()

        msgs = [
            "🔄 *All clear!* Today's session has been reset.\n\nWhenever you're ready, tap *▶️ Start Working* to begin fresh! 🌅",
            "✨ *Fresh start!* Session cleared.\n\nHit *▶️ Start Working* when you're ready to go! 💪",
            "🌱 *Reset done!* Timeline cleared, goal kept.\n\nPress *▶️ Start Working* to begin! 🚀",
        ]
        await u.message.reply_text(
            random.choice(msgs),
            parse_mode="Markdown",
            reply_markup=kb_idle(),
        )
    finally:
        db.close()


async def cmd_report(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "📊 *Work Reports*\n\nHere's a summary of your hours. Choose a timeframe:",
        parse_mode="Markdown",
        reply_markup=kb_reports_menu(),
    )


async def cmd_export(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "📤 *Export Work History*\n\nChoose your format and time range — I'll prepare the file instantly:",
        parse_mode="Markdown",
        reply_markup=kb_export_menu(),
    )


async def cmd_holiday(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "🗓 *Holidays & Days Off*\n\nAdd dates when you're not working — I'll skip reminders and stop expecting work on those days.",
        parse_mode="Markdown",
        reply_markup=kb_holiday_menu(),
    )


# ── /setgoal conversation ──────────────────────────────────────

async def cmd_setgoal(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if ctx.args:
        return await _apply_goal(u, ctx, ctx.args[0])
    await u.message.reply_text(
        "🎯 *Set your daily work goal*\n\nHow many hours do you aim to work each day?\nJust type a number like `6`, `7.5`, or `8`:",
        parse_mode="Markdown",
    )
    return ASK_GOAL


async def _goal_received(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await _apply_goal(u, ctx, u.message.text)


async def _apply_goal(u: Update, ctx: ContextTypes.DEFAULT_TYPE, raw: str) -> int:
    try:
        hours = float(raw.strip().replace("h", "").replace("hours", ""))
        assert 0 < hours <= 24
    except (ValueError, AssertionError):
        await u.message.reply_text(
            "🤔 *Hmm, that doesn't look right.*\n\nPlease enter a number between 1 and 24, like `6`, `7.5`, or `8`.",
            parse_mode="Markdown",
        )
        return ASK_GOAL

    uid = u.effective_user.id
    db  = get_db()
    try:
        user = get_or_create_user(db, uid)
        user.goal_hours = hours
        # Update all working-day schedules
        for sched in user.schedules:
            if sched.is_working_day:
                sched.required_hours = hours
        db.commit()
    finally:
        db.close()

    goal_msgs = [
        f"✅ *Goal locked in: {hours:g}h per day!*\n\nI'll celebrate every milestone and shout the moment you hit it! 🎉",
        f"🎯 *{hours:g} hours — let's go!* Goal saved.\n\nExpect milestone cheers at 25%, 50%, 75%, and a big 🎉 when you finish!",
        f"✨ *Perfect!* Daily goal set to *{hours:g}h*.\n\nI'll track every minute and notify you right when you're done! 🏆",
    ]
    await u.message.reply_text(
        random.choice(goal_msgs),
        parse_mode="Markdown",
        reply_markup=kb_idle(),
    )
    return ConversationHandler.END


async def _cancel_conv(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await u.message.reply_text(
        "No problem! Cancelled. 👍",
        reply_markup=kb_idle(),
    )
    return ConversationHandler.END


# ── /edit conversation ─────────────────────────────────────────

async def cmd_edit(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await u.message.reply_text(
        "✏️ *Correct your session*\n\nMade a mistake? No worries — pick what you'd like to fix:",
        parse_mode="Markdown",
        reply_markup=kb_edit_menu(),
    )


# ── Report helpers ─────────────────────────────────────────────

def _weekly_report(db, user: User, tz: ZoneInfo) -> str:
    today = _now_local(tz).date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week   = start_of_week + timedelta(days=6)
    sessions = get_sessions_for_range(db, user.id, start_of_week, today)

    from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
    ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
    le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))

    total_worked = 0.0
    days_worked  = 0
    goals_met    = 0

    by_date: dict[date, WorkSession] = {}
    for s in sessions:
        if s.date not in by_date or s.started_at < by_date[s.date].started_at:
            by_date[s.date] = s

    day_lines = []
    weekly_target = 0.0
    for i in range(7):
        d = start_of_week + timedelta(days=i)
        sched = get_schedule_for_weekday(db, user.id, d.weekday())
        req = sched.required_hours if sched and sched.required_hours else user.goal_hours
        if sched and sched.is_working_day:
            weekly_target += req * 3600

        sess = by_date.get(d)
        if sess:
            result = _calc_session(sess, sched, tz, time(ls_h, ls_m), time(le_h, le_m))
            total_worked += result.worked_secs
            days_worked  += 1
            if result.is_complete:
                goals_met += 1
                badge = "✅"
            else:
                badge = "⏳" if d == today else "⚡"
            day_lines.append(f"• *{WEEKDAY_NAMES[i][:3]}, {d.strftime('%b %d')}:* {fmt_dur(result.worked_secs)} {badge}")
        else:
            if d > today:
                day_lines.append(f"• *{WEEKDAY_NAMES[i][:3]}, {d.strftime('%b %d')}:* —")
            elif sched and sched.is_working_day:
                day_lines.append(f"• *{WEEKDAY_NAMES[i][:3]}, {d.strftime('%b %d')}:* 0m")
            else:
                day_lines.append(f"• *{WEEKDAY_NAMES[i][:3]}, {d.strftime('%b %d')}:* _day off_")

    pct = min(100, int(total_worked / weekly_target * 100)) if weekly_target else 0
    avg = total_worked / days_worked if days_worked else 0

    return "\n".join([
        f"📊 *Weekly Report* ({start_of_week.strftime('%b %d')} – {end_of_week.strftime('%b %d')})",
        "",
        f"⏱ Total Worked: *{fmt_dur(total_worked)}*",
        f"🎯 Weekly Target: *{fmt_dur(weekly_target)}* ({pct}%)",
        f"📈 Daily Average: *{fmt_dur(avg)}*",
        f"🏆 Goals Hit: *{goals_met}/{max(days_worked, 1)} working days*",
        "",
        pbar(pct),
        "",
        "*📅 Daily Breakdown:*",
        *day_lines,
    ])


def _monthly_report(db, user: User, tz: ZoneInfo) -> str:
    today = _now_local(tz).date()
    from calendar import monthrange
    year, month = today.year, today.month
    start_of_month = date(year, month, 1)
    sessions = get_sessions_for_range(db, user.id, start_of_month, today)

    from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
    ls_h, ls_m = map(int, DEFAULT_LUNCH_START.split(":"))
    le_h, le_m = map(int, DEFAULT_LUNCH_END.split(":"))

    total_worked = 0.0
    days_worked  = 0
    goals_met    = 0

    for sess in sessions:
        sched = get_schedule_for_weekday(db, user.id, sess.date.weekday())
        result = _calc_session(sess, sched, tz, time(ls_h, ls_m), time(le_h, le_m))
        total_worked += result.worked_secs
        days_worked  += 1
        if result.is_complete:
            goals_met += 1

    avg         = total_worked / days_worked if days_worked else 0
    success_pct = int(goals_met / days_worked * 100) if days_worked else 0

    return "\n".join([
        f"📆 *Monthly Report* ({today.strftime('%B %Y')})",
        "",
        f"⏱ Total Worked: *{fmt_dur(total_worked)}*",
        f"💼 Active Days: *{days_worked}*",
        f"📈 Average/Day: *{fmt_dur(avg)}*",
        f"🎯 Goals Hit: *{goals_met}/{days_worked}* ({success_pct}%)",
        f"⭐ Daily Goal: *{fmt_dur(user.goal_hours * 3600)}*",
    ])


# ── Export helpers ─────────────────────────────────────────────

async def _send_export(bot, chat_id: int, uid: int, fmt: str, period: str):
    from exporter import export_csv, export_excel
    db = get_db()
    try:
        user  = get_or_create_user(db, uid)
        tz    = _user_tz(user)
        today = _now_local(tz).date()

        if period == "today":
            start, end = today, today
        elif period == "week":
            start = today - timedelta(days=today.weekday())
            end   = today
        elif period == "month":
            start = today.replace(day=1)
            end   = today
        else:
            start, end = today, today

        filename_period = period.capitalize()
        if fmt == "csv":
            data = export_csv(db, user, start, end)
            filename = f"WorkBot_{filename_period}_{today}.csv"
            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(data, filename=filename),
                caption=f"📄 Work history – {filename_period}\n_{start} → {end}_",
                parse_mode="Markdown",
            )
        else:
            data = export_excel(db, user, start, end)
            filename = f"WorkBot_{filename_period}_{today}.xlsx"
            await bot.send_document(
                chat_id=chat_id,
                document=InputFile(data, filename=filename),
                caption=f"📊 Work history – {filename_period}\n_{start} → {end}_",
                parse_mode="Markdown",
            )
    finally:
        db.close()


# ── Inline button handler ──────────────────────────────────────

async def on_button(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    uid     = u.effective_user.id
    chat_id = u.effective_chat.id
    data    = q.data

    db = get_db()
    try:
        user  = get_or_create_user(db, uid)
        user.id  # ensure loaded
        tz    = _user_tz(user)
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)

        async def say(text: str, kb=None):
            await q.message.reply_text(
                text, parse_mode="Markdown",
                reply_markup=kb or kb_for_session(sess),
            )

        async def edit(text: str, kb=None):
            try:
                await q.edit_message_text(
                    text, parse_mode="Markdown",
                    reply_markup=kb or kb_for_session(sess),
                )
            except BadRequest:
                await say(text, kb)

        # ── Begin ──
        if data == "begin":
            db.close()  # reopen inside _do_begin
            async def reply(text, kb):
                await say(text, kb)
            await _do_begin(uid, chat_id, ctx.application, reply)
            return

        if data == "begin_extra":
            db.close()
            async def reply(text, kb):
                await say(text, kb)
            await _do_begin(uid, chat_id, ctx.application, reply, extra=True)
            return

        # ── Pause ──
        if data == "pause":
            if not sess or sess.status == "done":
                await say("👀 *No active session.*\n\nStart one first — tap below!", kb_idle()); return
            if sess.status in ("paused", "lunch"):
                await say("⏸ *You're already on a break!*\n\nTap *▶️ Resume Working* when you're back. ☕"); return
            if sess.status != "active":
                await say("🤔 Nothing to pause right now."); return
            now_utc = _now_utc()
            brk = Break(session_id=sess.id, started_at=now_utc, break_type="manual")
            db.add(brk)
            sess.status = "paused"
            db.commit()
            cancel_done_job(ctx.application, user.id)
            sched  = get_schedule_for_weekday(db, user.id, today.weekday())
            result = _get_session_result(db, sess, sched, tz)
            await say(
                r(PAUSE_MSGS).format(t=fmt_t(_now_local(tz)), worked=fmt_dur(result.worked_secs)),
                kb_paused(),
            )

        # ── Resume ──
        elif data == "resume":
            if not sess: await say("👀 *No active session.*\n\nStart one first!", kb_idle()); return
            if sess.status == "active": await say("💼 *You're already working!*\n\nNeed a break? Tap *⏸ Take a Break*.", kb_working()); return
            if sess.status not in ("paused", "lunch"):
                await say("🤔 Nothing to resume right now."); return
            open_brk = get_open_break(db, sess.id)
            if open_brk:
                open_brk.ended_at = _now_utc()
            sess.status = "active"
            db.commit()
            sched  = get_schedule_for_weekday(db, user.id, today.weekday())
            result = _get_session_result(db, sess, sched, tz)
            if result.projected_done_at:
                schedule_done_job(ctx.application, user.id, chat_id, tz, result.projected_done_at)
            await say(
                r(RESUME_MSGS).format(
                    t=fmt_t(_now_local(tz)),
                    worked=fmt_dur(result.worked_secs),
                    left=fmt_dur(result.remaining_secs),
                    bar=pbar(result.pct),
                ),
                kb_working(),
            )

        # ── Lunch override ──
        elif data == "lunch_override":
            if not sess: await say("👀 No session!", kb_idle()); return
            open_brk = get_open_break(db, sess.id)
            if open_brk:
                open_brk.ended_at = _now_utc()
            sess.status = "active"
            db.commit()
            sched  = get_schedule_for_weekday(db, user.id, today.weekday())
            result = _get_session_result(db, sess, sched, tz)
            if result.projected_done_at:
                schedule_done_job(ctx.application, user.id, chat_id, tz, result.projected_done_at)
            await say(
                "🟢 *Working through lunch — got it!*\n\n"
                f"⏱ Worked: *{fmt_dur(result.worked_secs)}*  ·  ⏳ Left: *{fmt_dur(result.remaining_secs)}*\n\n"
                f"{pbar(result.pct)}\n\nLunch break skipped. Timer is running! 💪",
                kb_working(),
            )

        # ── Status ──
        elif data == "status":
            if not sess:
                await say("👋 *No session today yet.*\n\nTap *▶️ Start Working* when you're ready!", kb_idle()); return
            sched  = get_schedule_for_weekday(db, user.id, today.weekday())
            result = _get_session_result(db, sess, sched, tz)
            card   = _status_card(sess, result, tz, sched)
            tl     = _timeline_text(sess, tz)
            await say(card + "\n\n" + tl)

        # ── Done ──
        elif data == "done":
            if not sess: await say("👋 *No active session.*", kb_idle()); return
            now_utc  = _now_utc()
            open_brk = get_open_break(db, sess.id)
            if open_brk:
                open_brk.ended_at = now_utc
            sess.status = "done"
            sess.ended_at = now_utc
            db.commit()
            stop_safety_net(ctx.application, user.id)
            cancel_done_job(ctx.application, user.id)
            sched  = get_schedule_for_weekday(db, user.id, today.weekday())
            result = _get_session_result(db, sess, sched, tz)
            card   = _status_card(sess, result, tz, sched)
            tl     = _timeline_text(sess, tz)
            if result.is_complete:
                overtime = result.worked_secs - result.required_secs
                ot_str = f"\n🌟 Overtime: *{fmt_dur(overtime)}* — above and beyond!" if overtime > 60 else ""
                congrats = (
                    f"🎉 *Day complete! Outstanding work!*\n"
                    f"You hit your *{fmt_dur(result.required_secs)}* goal.{ot_str}\n\n"
                )
            else:
                congrats = "🏁 *Session closed.*\n\nEvery minute counts. See you next time! 💙\n\n"
            await say(congrats + card + "\n\n" + tl, kb_done())

        # ── Reset ──
        elif data == "reset":
            stop_safety_net(ctx.application, user.id)
            cancel_done_job(ctx.application, user.id)
            if sess and sess.status != "done":
                now_utc  = _now_utc()
                open_brk = get_open_break(db, sess.id)
                if open_brk:
                    open_brk.ended_at = now_utc
                sess.status = "done"
                if not sess.ended_at:
                    sess.ended_at = now_utc
                db.commit()
            await say(
                random.choice([
                    "🔄 *All clear!* Session reset.\nHit *▶️ Start Working* when you're ready! 🌅",
                    "✨ *Fresh start!* Timeline cleared.\nTap *▶️ Start Working* to begin again! 💪",
                    "🌱 *Reset done!* Ready for a new session.\nPress *▶️ Start Working* to go! 🚀",
                ]),
                kb_idle(),
            )

        # ── Set goal ──
        elif data == "setgoal":
            ctx.user_data["awaiting_goal"] = True
            await say(
                "🎯 *Set your daily work goal*\n\nHow many hours do you aim to work each day?\nJust type a number like `6`, `7.5`, or `8`:",
                InlineKeyboardMarkup([]),
            )

        # ── Reports ──
        elif data == "reports_menu":
            await edit("📊 *Work Reports*\n\nHere's your work history at a glance. Choose a timeframe:", kb_reports_menu())

        elif data == "report_week":
            text = _weekly_report(db, user, tz)
            await edit(text, kb_reports_menu())

        elif data == "report_month":
            text = _monthly_report(db, user, tz)
            await edit(text, kb_reports_menu())

        # ── Export ──
        elif data == "export_menu":
            await edit("📤 *Export Work History*\n\nChoose your format and period — I'll prepare the file instantly:", kb_export_menu())

        elif data.startswith("export_"):
            parts  = data.split("_")   # export_csv_today or export_xlsx_week
            fmt    = parts[1]           # csv | xlsx
            period = parts[2]           # today | week | month
            period_label = {"today": "Today", "week": "This Week", "month": "This Month"}.get(period, period.capitalize())
            await q.message.reply_text(
                f"⏳ *Preparing your {fmt.upper()} file for {period_label}...*\n_This takes just a second!_",
                parse_mode="Markdown",
            )
            await _send_export(ctx.bot, chat_id, uid, fmt, period)

        # ── Edit menu ──
        elif data == "edit_menu":
            await say("✏️ *Correct your session*\n\nMade a mistake? No worries — pick what you'd like to fix:", kb_edit_menu())

        elif data == "edit_start":
            ctx.user_data["edit_action"] = "start"
            await say(
                "🕐 *Change Start Time*\n\nWhat time did you actually start?\nEnter it in *HH:MM* format (e.g. `09:20`):",
                InlineKeyboardMarkup([]),
            )

        elif data == "edit_end":
            ctx.user_data["edit_action"] = "end"
            await say(
                "🕔 *Change End Time*\n\nWhat time did you actually finish?\nEnter it in *HH:MM* format (e.g. `18:05`):",
                InlineKeyboardMarkup([]),
            )

        elif data == "edit_add_break":
            ctx.user_data["edit_action"] = "add_break"
            await say(
                "➕ *Add a Manual Break*\n\nEnter the break period in *HH:MM-HH:MM* format\n(e.g. `12:00-12:30` for a 30-minute break):",
                InlineKeyboardMarkup([]),
            )

        elif data == "edit_delete":
            if not sess:
                await say("👀 *No session to delete.*\n\nStart one below!", kb_idle()); return
            now_utc = _now_utc()
            open_brk = get_open_break(db, sess.id)
            if open_brk:
                open_brk.ended_at = now_utc
            sess.status = "done"
            sess.ended_at = now_utc
            db.commit()
            stop_safety_net(ctx.application, user.id)
            cancel_done_job(ctx.application, user.id)
            await say(
                "🗑 *Session deleted.*\n\nAll cleared! Start a fresh one whenever you're ready.",
                kb_idle(),
            )

        # ── Holidays ──
        elif data == "holiday_menu":
            await edit(
                "🗓 *Holidays & Days Off*\n\nAdd dates when you're not working — I'll skip reminders and stop expecting work on those days.",
                kb_holiday_menu(),
            )

        elif data == "holiday_add":
            ctx.user_data["awaiting_holiday_date"] = True
            await say(
                "🗓 *Add a Day Off*\n\nEnter the date you want to mark as non-working:\n*YYYY-MM-DD* format (e.g. `2026-12-25`):",
                InlineKeyboardMarkup([]),
            )

        elif data == "holiday_list":
            from db import Holiday as HolidayModel
            holidays = (
                db.query(HolidayModel)
                .filter(HolidayModel.user_id == user.id)
                .order_by(HolidayModel.date)
                .all()
            )
            if not holidays:
                await say(
                    "📋 *No days off added yet.*\n\nTap *➕ Add Day Off* to mark holidays, vacations, or any other non-working days.",
                    kb_holiday_menu(),
                )
            else:
                lines = ["📋 *Your Days Off:*", ""]
                for h in holidays:
                    reason = f"  —  _{h.reason}_" if h.reason else ""
                    lines.append(f"🗓 *{h.date.strftime('%b %d, %Y')}*{reason}")
                lines += ["", f"_Total: {len(holidays)} day(s) off configured._"]
                await say("\n".join(lines), kb_holiday_menu())

        # ── Schedule view ──
        elif data == "view_schedule":
            lines = ["📅 *Your Work Schedule*", ""]
            for wd in range(7):
                sched = get_schedule_for_weekday(db, user.id, wd)
                day_name = WEEKDAY_NAMES[wd]
                if sched and sched.is_working_day:
                    start_str = sched.start_time.strftime("%H:%M") if sched.start_time else "09:00"
                    end_str   = sched.end_time.strftime("%H:%M")   if sched.end_time   else "18:00"
                    req_str   = fmt_dur(sched.required_hours * 3600) if sched.required_hours else fmt_dur(user.goal_hours * 3600)
                    lines.append(f"✅  *{day_name}:*  {start_str}–{end_str}  _({req_str} required)_")
                else:
                    lines.append(f"🛵  *{day_name}:*  Day off")
            from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
            lines += [
                "",
                f"🍱  Lunch break: *{DEFAULT_LUNCH_START}–{DEFAULT_LUNCH_END}* (automatic)",
                f"🎯  Daily goal: *{fmt_dur(user.goal_hours * 3600)}*",
            ]
            await say("\n".join(lines), kb_for_session(sess))

        # ── Back ──
        elif data == "back_main":
            if sess:
                sched  = get_schedule_for_weekday(db, user.id, today.weekday())
                result = _get_session_result(db, sess, sched, tz)
                await edit(_status_card(sess, result, tz, sched), kb_for_session(sess))
            else:
                await edit(
                    "👋 *Ready when you are!*\n\nTap *▶️ Start Working* to begin your day. 💪",
                    kb_idle(),
                )

    finally:
        try:
            db.close()
        except Exception:
            pass


# ── Free text handler ──────────────────────────────────────────

async def on_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = u.message.text or ""
    uid  = u.effective_user.id
    db   = get_db()
    try:
        user = get_or_create_user(db, uid)
        tz   = _user_tz(user)

        # ── Awaiting goal ──
        if ctx.user_data.get("awaiting_goal"):
            ctx.user_data.pop("awaiting_goal")
            await _apply_goal(u, ctx, text)
            return

        # ── Awaiting holiday date ──
        if ctx.user_data.get("awaiting_holiday_date"):
            ctx.user_data.pop("awaiting_holiday_date")
            try:
                h_date = date.fromisoformat(text.strip())
            except ValueError:
                await u.message.reply_text(
                    "❌ *Hmm, I don’t recognize that date.*\n\nPlease use *YYYY-MM-DD* format, e.g. `2026-12-25`.",
                    parse_mode="Markdown",
                )
                return
            ctx.user_data["holiday_date"] = h_date
            ctx.user_data["awaiting_holiday_reason"] = True
            await u.message.reply_text(
                f"🗓 *{h_date.strftime('%B %d, %Y')}* — got it!\n\nAnything to label it with? (e.g. `National Holiday`, `Vacation`, `Company Holiday`)\nOr type *skip* to leave it blank:",
                parse_mode="Markdown",
            )
            return

        # ── Awaiting holiday reason ──
        if ctx.user_data.get("awaiting_holiday_reason"):
            ctx.user_data.pop("awaiting_holiday_reason")
            h_date  = ctx.user_data.pop("holiday_date", None)
            reason  = None if text.strip().lower() in ("skip", "-", "") else text.strip()
            if h_date:
                from db import Holiday as HolidayModel
                from sqlalchemy.exc import IntegrityError
                try:
                    holiday = HolidayModel(user_id=user.id, date=h_date, reason=reason)
                    db.add(holiday)
                    db.commit()
                    reason_str = f"\n_Reason: {reason}_" if reason else ""
                    await u.message.reply_text(
                        f"✅ *{h_date.strftime('%B %d, %Y')}* added as a day off!{reason_str}\n\n"
                        f"I won't send work reminders on this date. 💫",
                        parse_mode="Markdown",
                        reply_markup=kb_holiday_menu(),
                    )
                except IntegrityError:
                    db.rollback()
                    await u.message.reply_text(
                        f"🗓 *{h_date.strftime('%B %d, %Y')}* is already in your days-off list.",
                        parse_mode="Markdown",
                    )
            return

        # ── Awaiting edit value ──
        edit_action = ctx.user_data.pop("edit_action", None)
        if edit_action:
            today = _now_local(tz).date()
            sess  = get_active_session(db, user.id, today)
            if not sess:
                await u.message.reply_text("👀 No session to edit.", reply_markup=kb_idle())
                return

            if edit_action in ("start", "end"):
                try:
                    h, m = map(int, text.strip().split(":"))
                    new_time = time(h, m)
                    new_dt   = datetime.combine(today, new_time, tzinfo=tz)
                    new_utc  = _to_utc(new_dt)
                    if edit_action == "start":
                        sess.started_at = new_utc
                        db.commit()
                        await u.message.reply_text(
                            f"✅ *Start time updated to {fmt_t(new_dt)}!*\n\nAll times have been recalculated automatically. 💫",
                            parse_mode="Markdown",
                            reply_markup=kb_for_session(sess),
                        )
                    else:
                        sess.ended_at = new_utc
                        if sess.status != "done":
                            sess.status = "done"
                        db.commit()
                        await u.message.reply_text(
                            f"✅ *End time updated to {fmt_t(new_dt)}!*\n\nSession summary recalculated.",
                            parse_mode="Markdown",
                            reply_markup=kb_done(),
                        )
                except (ValueError, AttributeError):
                    await u.message.reply_text(
                        "❌ *I couldn’t parse that time.*\n\nPlease use *HH:MM* format, e.g. `09:20` or `17:45`.",
                        parse_mode="Markdown",
                    )
            elif edit_action == "add_break":
                try:
                    parts = text.strip().split("-")
                    sh, sm = map(int, parts[0].strip().split(":"))
                    eh, em = map(int, parts[1].strip().split(":"))
                    b_start = _to_utc(datetime.combine(today, time(sh, sm), tzinfo=tz))
                    b_end   = _to_utc(datetime.combine(today, time(eh, em), tzinfo=tz))
                    brk = Break(
                        session_id=sess.id,
                        started_at=b_start,
                        ended_at=b_end,
                        break_type="manual",
                    )
                    db.add(brk)
                    db.commit()
                    await u.message.reply_text(
                        f"✅ *Break added!*  {time(sh, sm).strftime('%H:%M')}–{time(eh, em).strftime('%H:%M')}\n"
                        f"Duration: *{fmt_dur((time(eh, em).hour*60+time(eh, em).minute - time(sh, sm).hour*60-time(sh, sm).minute)*60)}*\n\nYour working time has been recalculated. 💫",
                        parse_mode="Markdown",
                        reply_markup=kb_for_session(sess),
                    )
                except (ValueError, IndexError):
                    await u.message.reply_text(
                        "❌ *Couldn’t parse that.*\n\nUse *HH:MM-HH:MM* format, e.g. `12:00-12:30`.",
                        parse_mode="Markdown",
                    )
            return

        # ── Default text response ──
        today = _now_local(tz).date()
        sess  = get_active_session(db, user.id, today)
        if sess and sess.status not in ("done", None):
            await u.message.reply_text(
                "👇 *Use the buttons below* to manage your session!",
                parse_mode="Markdown",
                reply_markup=kb_for_session(sess),
            )
        else:
            await u.message.reply_text(
                "👋 *Hey!* Tap *▶️ Start Working* to begin tracking your day!",
                parse_mode="Markdown",
                reply_markup=kb_idle(),
            )
    finally:
        db.close()


# ── App startup ────────────────────────────────────────────────

async def post_init(app: Application):
    await restore_scheduler_jobs(app)
    logger.info("WorkBot started successfully.")


# ── Main ──────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set in .env")
        return

    # Initialize DB
    init_db()

    # Run migration if needed
    run_migration()

    # Build application
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    # ── Conversation handlers ──
    goal_conv = ConversationHandler(
        entry_points=[CommandHandler("setgoal", cmd_setgoal)],
        states={ASK_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, _goal_received)]},
        fallbacks=[CommandHandler("cancel", _cancel_conv)],
    )

    # ── Command handlers ──
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("begin",   cmd_begin))
    app.add_handler(CommandHandler("start_work", cmd_begin))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("stop",    cmd_done))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_handler(CommandHandler("holiday", cmd_holiday))
    app.add_handler(CommandHandler("edit",    cmd_edit))
    app.add_handler(goal_conv)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("🤖 WorkBot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
