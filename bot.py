"""
WorkBot — Friendly Telegram Daily Working Hours Tracker
• Auto-notifies when goal is reached 🎉
• Milestone celebrations at 25 / 50 / 75 %
• Randomised, warm, human responses for every action
• Gentle error messages — no harsh warnings
• Preserves user's custom saved goal across resets & deployments
• Automatic lunch break pause from 13:00 to 14:00
• Server-ready: Persists active sessions so restarts don't lose working hours
• Weekly & Monthly Reports with interactive buttons 📊
"""

import os
import json
import random
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
LOCAL_TZ = ZoneInfo("Asia/Tashkent")   # UTC+5  ← change if needed

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.WARNING,
)
logger = logging.getLogger(__name__)

ASK_GOAL = 0
DATA_FILE = os.path.join(os.path.dirname(__file__), "user_data.json")

# ── Serialization Helpers ─────────────────────────────────────
def now_local() -> datetime:
    return datetime.now(tz=LOCAL_TZ)

def is_lunch_time(dt: datetime | None = None) -> bool:
    t = dt or now_local()
    return 13 <= t.hour < 14

def serialize_session(sess: dict) -> dict:
    return {
        "goal_hours": sess["goal_hours"],
        "status": sess["status"],
        "start_time": sess["start_time"].isoformat() if sess["start_time"] else None,
        "timeline": [
            {
                "type": s["type"],
                "start": s["start"].isoformat() if s.get("start") else None,
                "end": s["end"].isoformat() if s.get("end") else None,
            }
            for s in sess.get("timeline", [])
        ],
        "current_seg": {
            "type": sess["current_seg"]["type"],
            "start": sess["current_seg"]["start"].isoformat() if sess["current_seg"].get("start") else None,
            "end": sess["current_seg"]["end"].isoformat() if sess["current_seg"].get("end") else None,
        } if sess.get("current_seg") else None,
        "chat_id": sess.get("chat_id"),
        "milestones_sent": list(sess.get("milestones_sent", [])),
        "auto_lunch_paused": sess.get("auto_lunch_paused", False),
        "lunch_override": sess.get("lunch_override", False),
    }

def deserialize_session(d: dict) -> dict:
    def parse_dt(s):
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=LOCAL_TZ)
        return dt

    return {
        "goal_hours": float(d.get("goal_hours", 6.0)),
        "status": d.get("status", "idle"),
        "start_time": parse_dt(d.get("start_time")),
        "timeline": [
            {
                "type": s["type"],
                "start": parse_dt(s["start"]),
                "end": parse_dt(s["end"]),
            }
            for s in d.get("timeline", [])
        ],
        "current_seg": {
            "type": d["current_seg"]["type"],
            "start": parse_dt(d["current_seg"]["start"]),
            "end": parse_dt(d["current_seg"]["end"]),
        } if d.get("current_seg") else None,
        "chat_id": d.get("chat_id"),
        "milestones_sent": set(d.get("milestones_sent", [])),
        "auto_lunch_paused": d.get("auto_lunch_paused", False),
        "lunch_override": d.get("lunch_override", False),
    }

# ── Data Persistence ──────────────────────────────────────────
def load_data() -> tuple[dict[int, float], dict[int, dict], dict[int, dict]]:
    if not os.path.exists(DATA_FILE):
        return {}, {}, {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
            goals = {int(k): float(v) for k, v in raw.get("goals", {}).items()}
            loaded_sessions = {}
            for k, v in raw.get("sessions", {}).items():
                try:
                    loaded_sessions[int(k)] = deserialize_session(v)
                except Exception as e:
                    logger.warning("Error deserializing session %s: %s", k, e)
            history = {int(k): v for k, v in raw.get("history", {}).items()}
            return goals, loaded_sessions, history
    except Exception as e:
        logger.warning("Error loading %s: %s", DATA_FILE, e)
        return {}, {}, {}

def save_data():
    try:
        data = {
            "goals": {str(k): v for k, v in user_goals.items()},
            "sessions": {str(k): serialize_session(v) for k, v in sessions.items()},
            "history": {str(k): v for k, v in user_history.items()},
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Error saving %s: %s", DATA_FILE, e)

user_goals, sessions, user_history = load_data()

def save_user_goal(uid: int, goal_hours: float):
    user_goals[uid] = goal_hours
    save_data()

def record_day_history(uid: int):
    sess = get_sess(uid)
    d_str = now_local().strftime("%Y-%m-%d")
    s = calc(sess)
    if uid not in user_history:
        user_history[uid] = {}
    user_history[uid][d_str] = {
        "worked": s["worked"],
        "resting": s["resting"],
        "goal": s["goal"],
        "completed": s["remaining"] == 0,
    }
    save_data()

# ── Per-user state ────────────────────────────────────────────
def get_sess(uid: int) -> dict:
    if uid not in sessions:
        sessions[uid] = {
            "goal_hours": user_goals.get(uid, 6.0),
            "status": "idle",          # idle | working | paused | done
            "start_time": None,
            "timeline": [],
            "current_seg": None,
            "chat_id": None,
            "milestones_sent": set(),
            "auto_lunch_paused": False,
            "lunch_override": False,
        }
        save_data()
    return sessions[uid]

def reset_session(uid: int, chat_id: int | None = None) -> dict:
    user_goal = user_goals.get(uid, sessions.get(uid, {}).get("goal_hours", 6.0))
    sess = {
        "goal_hours": user_goal,
        "status": "idle",
        "start_time": None,
        "timeline": [],
        "current_seg": None,
        "chat_id": chat_id,
        "milestones_sent": set(),
        "auto_lunch_paused": False,
        "lunch_override": False,
    }
    sessions[uid] = sess
    save_data()
    return sess

# ── Randomised response bank ──────────────────────────────────
def r(pool: list[str]) -> str:
    return random.choice(pool)

GREET = [
    "Hey {name}! 👋 So glad you're here!",
    "Welcome back, {name}! 😄 Ready to have a productive day?",
    "Hi {name}! 🌟 Let's make today count!",
    "Good to see you, {name}! 💪 Let's get to work!",
]

BEGIN = [
    "🚀 *Off we go!* Timer started at *{t}*\n🎯 Goal for today: *{goal}*\n\nI've got your back — I'll cheer you on at every milestone and shout the moment you're done! 🎉",
    "▶️ *Let's do this!* Clocked in at *{t}*\n🎯 Today's goal: *{goal}*\n\nSit tight — I'll ping you at 25%, 50%, 75%, and *celebrate* when you hit 100%! 🏆",
    "💼 *Work mode: ON!* Started at *{t}*\n🎯 You're going for *{goal}* today!\n\nI'll be watching your progress and give you a shout when it's time to celebrate! 🎊",
    "⏱ *Timer running!* Clock-in: *{t}*\n🎯 Mission: *{goal}* of focused work!\n\nYou've got this. I'll celebrate every milestone with you! 🌟",
]

PAUSE = [
    "⏸ *Break time — you've earned it!* ☕\nBreak started: *{t}*\nWorked so far: *{worked}*\n\nRelax, grab a coffee, stretch a little. I'll be here when you're back! 😊",
    "☕ *Enjoy your break!* Paused at *{t}*\nGreat progress so far: *{worked}* in the bag!\n\nRest up — hit *▶️ Resume* whenever you're ready! 🙌",
    "🧘 *Rest mode!* Taking a break at *{t}*\nYou've already clocked *{worked}* — nice work!\n\nStep away, breathe, come back fresh! 💆",
    "⏸ *Pausing the clock at {t}* — well deserved!\nSo far: *{worked}* done!\n\nTake your time, I'm not going anywhere 😄",
]

RESUME = [
    "▶️ *Welcome back!* Ready to keep crushing it? 💪\nResumed at: *{t}*\n⏱ Worked: *{worked}* | ⏳ Remaining: *{left}*\n\n{bar}",
    "🔥 *Back in the zone!* Resumed at *{t}*\n⏱ Done so far: *{worked}* | ⏳ Left: *{left}*\n\n{bar}\n\nYou're doing amazing — keep going!",
    "💼 *And we're back!* Clocked in again at *{t}*\n⏱ Progress: *{worked}* | ⏳ To go: *{left}*\n\n{bar}\n\nLet's finish strong! 🏁",
    "⚡ *Game on!* Resumed at *{t}*\n⏱ Worked: *{worked}* | ⏳ Remaining: *{left}*\n\n{bar}",
]

DONE_GOAL = [
    "🏆 *YOU DID IT!* Full *{goal}* goal smashed today!\n\n🕐 Started: *{start}* → Finished: *{end}*\n⏱ Worked: *{worked}* | ☕ Breaks: *{rest}*\n\n{bar}\n\n🎊 *Seriously — well done!* Go rest, you've earned it! 🌙\n\n{timeline}",
    "🎉 *GOAL ACHIEVED!* You crushed your *{goal}* target!\n\n🕐 {start} → {end}\n⏱ Net work: *{worked}* | ☕ Rest: *{rest}*\n\n{bar}\n\n*That's what dedication looks like!* 🔥 Enjoy your evening! 🌙\n\n{timeline}",
    "🏅 *MISSION COMPLETE!* *{goal}* of great work done!\n\n🕐 Started: *{start}* | Wrapped: *{end}*\n⏱ Total worked: *{worked}* | ☕ Breaks: *{rest}*\n\n{bar}\n\n*Be proud of yourself today!* 💙\n\n{timeline}",
]

DONE_PARTIAL = [
    "🏁 *Day wrapped!*\n\n🕐 {start} → {end}\n⏱ Worked: *{worked}* | ☕ Breaks: *{rest}*\n🎯 Goal was: *{goal}*\n\n{bar}\n\n_Every bit of effort counts. See you tomorrow! 💙_\n\n{timeline}",
    "🌅 *Session closed!*\n\n🕐 {start} → {end}\n⏱ You put in *{worked}* today — that's real! ☕ Rest: *{rest}*\n🎯 Goal: *{goal}*\n\n{bar}\n\n_Progress over perfection. Great job! 🤝_\n\n{timeline}",
]

ALREADY_WORKING = [
    "😄 Hey, you're already on the clock! Use the buttons to pause, check status, or finish your day.",
    "⏱ Timer's already running! No need to start again — use *⏸ Pause* or *📊 My Status* below.",
    "💼 You're already in work mode! Tap *📊 My Status* to see how you're doing.",
]

NOT_WORKING = [
    "🤔 Looks like you're not working right now! Hit *▶️ Start Working* to begin your session.",
    "😴 No active session yet! Tap *▶️ Start Working* to kick things off.",
    "👀 Timer hasn't started yet! Press *▶️ Start Working* when you're ready.",
]

NOT_ON_BREAK = [
    "🙂 You're not on a break right now — you're working! Use *⏸ Pause* if you need a rest.",
    "💡 You're still in work mode! Hit *⏸ Pause / Break* when you want to step away.",
    "😄 No break is running! You're actively working. Tap *⏸ Pause* anytime you need a breather.",
]

ALREADY_PAUSED = [
    "☕ You're already on a break! Hit *▶️ Resume Working* when you're ready to continue.",
    "⏸ Break is already in progress! Tap *▶️ Resume* when you're back.",
    "🧘 You're resting right now! Press *▶️ Resume Working* to get back to it.",
]

NO_SESSION = [
    "👋 No active session found! Tap *▶️ Start Working* to begin tracking your day.",
    "😊 Looks like you haven't started yet! Hit *▶️ Start Working* to begin.",
    "🌅 Nothing to show yet! Start a session first with *▶️ Start Working*.",
]

MILESTONE = {
    25: [
        "🌱 *25% done!* You're off to a great start! Keep that energy going! ⚡",
        "✨ *Quarter way there!* Solid beginning — you've got this! 💪",
        "🎯 *25% complete!* One step at a time — you're building momentum! 🔥",
    ],
    50: [
        "🔥 *HALFWAY THERE!* You're absolutely smashing it today! 💥",
        "⚡ *50% done!* Right in the middle — the finish line is getting closer! 🏁",
        "🎊 *Half the goal done!* You're on fire! Keep the momentum! 🚀",
    ],
    75: [
        "⚡ *75% complete!* Almost there — don't stop now! The end is so close! 🏆",
        "🏃 *Three quarters done!* One final push and you're there! You've got this! 💪",
        "🌟 *75% — incredible!* Just a little more and I'll be sending that goal notification! 🎉",
    ],
}

GOAL_REACHED = [
    "🎉🎉🎉 *HOORAY! YOU DID IT!*\n\nYou've just completed your *{goal}* daily goal! That's amazing!\n\n⏱ Total worked: *{worked}*\n☕ Break time: *{rest}*\n\n{bar}\n\n{timeline}\n\n🌙 *Rest well — you absolutely deserve it!* See you tomorrow! 💙",
    "🏆 *GOAL SMASHED!* Congratulations!\n\nYou've hit your full *{goal}* for today! Incredible work!\n\n⏱ Worked: *{worked}* | ☕ Breaks: *{rest}*\n\n{bar}\n\n{timeline}\n\n🎊 *Be proud of yourself — seriously!* Now go relax! 🛋",
    "🥳 *MISSION ACCOMPLISHED!*\n\nYou crushed your *{goal}* target today! Nothing can stop you!\n\n⏱ Net work: *{worked}* | ☕ Rest: *{rest}*\n\n{bar}\n\n{timeline}\n\n⭐ *Outstanding effort today!* You earned a proper rest! 🌙",
]

# ── Helpers ───────────────────────────────────────────────────
def fmt_t(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "—"

def fmt_dur(secs: float) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"

def pbar(pct: int, w: int = 14) -> str:
    f = int(w * pct / 100)
    return f"`{'▓'*f}{'░'*(w-f)}` *{pct}%*"

def calc(sess: dict) -> dict:
    now = now_local()
    worked = resting = 0.0
    for seg in sess["timeline"]:
        end = seg["end"] or now
        d = (end - seg["start"]).total_seconds()
        if seg["type"] == "work":
            worked += d
        else:
            resting += d
    if sess["current_seg"]:
        d = (now - sess["current_seg"]["start"]).total_seconds()
        if sess["current_seg"]["type"] == "work":
            worked += d
        else:
            resting += d
    goal = sess["goal_hours"] * 3600
    remaining = max(0.0, goal - worked)
    pct = min(100, int(worked / goal * 100)) if goal else 0
    finish = None
    if remaining > 0 and sess["status"] == "working":
        finish = datetime.fromtimestamp(now.timestamp() + remaining, tz=LOCAL_TZ)
    return dict(worked=worked, resting=resting, goal=goal,
                remaining=remaining, pct=pct, finish=finish)

def close_seg(sess: dict):
    if sess["current_seg"]:
        s = sess["current_seg"].copy()
        s["end"] = now_local()
        sess["timeline"].append(s)
        sess["current_seg"] = None

def open_seg(sess: dict, kind: str):
    close_seg(sess)
    sess["current_seg"] = {"type": kind, "start": now_local(), "end": None}

def timeline_text(sess: dict) -> str:
    segs = list(sess["timeline"])
    if sess["current_seg"]:
        segs.append({**sess["current_seg"], "_open": True})
    if not segs:
        return ""
    lines = ["*📅 Timeline*", ""]
    for s in segs:
        icon = "💼" if s["type"] == "work" else "☕"
        is_open = s.get("_open", False)
        end = s.get("end")
        end_str = "now ←" if is_open else fmt_t(end)
        d = ((end or now_local()) - s["start"]).total_seconds()
        lines.append(f"{icon}  {fmt_t(s['start'])} → {end_str}   _({fmt_dur(d)})_")
    return "\n".join(lines)

def status_card(sess: dict) -> str:
    s = calc(sess)
    status = sess["status"]
    if status == "paused" and sess.get("auto_lunch_paused") and is_lunch_time():
        emoji = "🍱"
        label = "Lunch Break (Paused until 14:00)"
    else:
        emoji = {"idle": "😴", "working": "💼", "paused": "☕", "done": "✅"}.get(status, "")
        label = {"idle": "Idle", "working": "Working", "paused": "On Break", "done": "Done"}.get(status, "")

    lines = [
        "*WorkBot — Your Day* 📊",
        "",
        f"{emoji}  Status: *{label}*   |   🎯 Goal: *{fmt_dur(s['goal'])}*",
        "",
        f"⏱  Worked:     *{fmt_dur(s['worked'])}*",
        f"☕  Breaks:     *{fmt_dur(s['resting'])}*",
        f"⏳  Remaining:  *{fmt_dur(s['remaining'])}*",
        "",
        pbar(s["pct"]),
    ]
    if s["finish"]:
        lines += ["", f"🏁  Finish by: *{fmt_t(s['finish'])}*"]
    if sess["status"] == "done" and s["remaining"] == 0:
        lines += ["", "🎉 *Goal achieved!*"]
    return "\n".join(lines)

# ── Reports Logic ─────────────────────────────────────────────
def get_weekly_report(uid: int) -> str:
    now = now_local()
    today = now.date()
    start_of_week = today - timedelta(days=today.weekday())
    end_of_week = start_of_week + timedelta(days=6)

    sess = get_sess(uid)
    hist = user_history.get(uid, {})

    total_worked = 0.0
    days_worked = 0
    goals_met = 0
    day_lines = []

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i in range(7):
        d = start_of_week + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        worked = 0.0
        goal = sess["goal_hours"] * 3600

        if d == today and sess["status"] in ("working", "paused", "done"):
            s = calc(sess)
            worked = s["worked"]
            goal = s["goal"]
        elif d_str in hist:
            entry = hist[d_str]
            worked = entry.get("worked", 0.0)
            goal = entry.get("goal", goal)

        total_worked += worked
        is_today = (d == today)
        is_future = (d > today)

        if worked > 0:
            days_worked += 1
            if worked >= goal:
                goals_met += 1
                badge = "✅"
            else:
                badge = "⏳" if is_today else "⚡"
            day_lines.append(f"• *{day_names[i]}, {d.strftime('%b %d')}:* {fmt_dur(worked)} {badge}")
        else:
            if is_today:
                day_lines.append(f"• *{day_names[i]}, {d.strftime('%b %d')}:* _in progress_ ⏳")
            elif is_future:
                day_lines.append(f"• *{day_names[i]}, {d.strftime('%b %d')}:* —")
            else:
                day_lines.append(f"• *{day_names[i]}, {d.strftime('%b %d')}:* 0m")

    weekly_target = sess["goal_hours"] * 3600 * 5
    pct = min(100, int(total_worked / weekly_target * 100)) if weekly_target else 0
    avg_worked = total_worked / days_worked if days_worked else 0.0

    lines = [
        f"📊 *Weekly Work Report* ({start_of_week.strftime('%b %d')} – {end_of_week.strftime('%b %d')})",
        "",
        f"⏱  Total Worked:    *{fmt_dur(total_worked)}*",
        f"🎯  Weekly Target:   *{fmt_dur(weekly_target)}* ({pct}%)",
        f"📈  Daily Average:   *{fmt_dur(avg_worked)}*",
        f"🏆  Goals Hit:       *{goals_met} / {days_worked if days_worked else 5} days*",
        "",
        pbar(pct),
        "",
        "*📅 Daily Breakdown:*",
        *day_lines,
    ]
    return "\n".join(lines)

def get_monthly_report(uid: int) -> str:
    now = now_local()
    today = now.date()
    month_name = now.strftime("%B %Y")
    year = now.year
    month = now.month

    sess = get_sess(uid)
    hist = user_history.get(uid, {})

    total_worked = 0.0
    days_worked = 0
    goals_met = 0

    for day_num in range(1, today.day + 1):
        d = date(year, month, day_num)
        d_str = d.strftime("%Y-%m-%d")
        worked = 0.0
        goal = sess["goal_hours"] * 3600

        if d == today and sess["status"] in ("working", "paused", "done"):
            s = calc(sess)
            worked = s["worked"]
            goal = s["goal"]
        elif d_str in hist:
            entry = hist[d_str]
            worked = entry.get("worked", 0.0)
            goal = entry.get("goal", goal)

        if worked > 0:
            total_worked += worked
            days_worked += 1
            if worked >= goal:
                goals_met += 1

    avg_worked = total_worked / days_worked if days_worked else 0.0
    success_rate = int(goals_met / days_worked * 100) if days_worked else 0

    lines = [
        f"📆 *Monthly Work Report* ({month_name})",
        "",
        f"⏱  Total Worked:      *{fmt_dur(total_worked)}*",
        f"💼  Active Days:       *{days_worked} days*",
        f"📈  Average / Day:     *{fmt_dur(avg_worked)}*",
        f"🎯  Days Target Met:   *{goals_met} / {days_worked}* ({success_rate}%)",
        f"⭐  Daily Goal:        *{fmt_dur(sess['goal_hours']*3600)}*",
    ]
    return "\n".join(lines)

# ── Keyboards ─────────────────────────────────────────────────
def kb_idle():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  Start Working", callback_data="begin")],
        [InlineKeyboardButton("🎯  Set Goal",      callback_data="setgoal"),
         InlineKeyboardButton("📈  Reports",       callback_data="reports_menu")],
    ])

def kb_working():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏸  Pause / Break", callback_data="pause"),
         InlineKeyboardButton("📊  My Status",     callback_data="status")],
        [InlineKeyboardButton("🏁  Finish Day",    callback_data="done"),
         InlineKeyboardButton("🔄  Reset",         callback_data="reset")],
    ])

def kb_paused():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  Resume Working", callback_data="resume"),
         InlineKeyboardButton("📊  My Status",      callback_data="status")],
        [InlineKeyboardButton("🏁  Finish Day",     callback_data="done"),
         InlineKeyboardButton("🔄  Reset",          callback_data="reset")],
    ])

def kb_lunch_paused():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️  Work Anyway", callback_data="lunch_override"),
         InlineKeyboardButton("📊  My Status",   callback_data="status")],
        [InlineKeyboardButton("🏁  Finish Day",  callback_data="done"),
         InlineKeyboardButton("🔄  Reset",       callback_data="reset")],
    ])

def kb_done():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄  Start New Day", callback_data="reset"),
         InlineKeyboardButton("📈  Reports",       callback_data="reports_menu")],
    ])

def kb_reports_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅  This Week",  callback_data="report_week"),
         InlineKeyboardButton("📆  This Month", callback_data="report_month")],
        [InlineKeyboardButton("🔙  Back",       callback_data="reports_back")],
    ])

def kb_report_view(current: str):
    if current == "week":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📆  Switch to Month", callback_data="report_month")],
            [InlineKeyboardButton("🔄  Refresh",         callback_data="report_week"),
             InlineKeyboardButton("🔙  Back",            callback_data="reports_back")],
        ])
    else:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📅  Switch to Week",  callback_data="report_week")],
            [InlineKeyboardButton("🔄  Refresh",         callback_data="report_month"),
             InlineKeyboardButton("🔙  Back",            callback_data="reports_back")],
        ])

def kb_for(sess: dict):
    st = sess["status"]
    if st == "idle":
        return kb_idle()
    if st == "working":
        return kb_working()
    if st == "paused":
        if sess.get("auto_lunch_paused") and is_lunch_time():
            return kb_lunch_paused()
        return kb_paused()
    if st == "done":
        return kb_done()
    return kb_idle()

# ── Job queue helpers ─────────────────────────────────────────
def start_job(app, uid: int, chat_id: int):
    name = f"track_{uid}"
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    app.job_queue.run_repeating(
        progress_job, interval=30, first=10,
        name=name, data={"uid": uid, "chat_id": chat_id}
    )

def stop_job(app, uid: int):
    for j in app.job_queue.get_jobs_by_name(f"track_{uid}"):
        j.schedule_removal()

async def progress_job(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    uid, chat_id = d["uid"], d["chat_id"]
    sess = sessions.get(uid)
    if not sess or sess["status"] not in ("working", "paused"):
        ctx.job.schedule_removal()
        return

    now = now_local()

    # ── Auto Lunch Pause (13:00 – 14:00) ──────────────────────────
    if sess["status"] == "working":
        if is_lunch_time(now) and not sess.get("lunch_override", False):
            open_seg(sess, "break")
            sess["status"] = "paused"
            sess["auto_lunch_paused"] = True
            save_data()
            msg = (
                "🍱 *Lunch Break Time (13:00 – 14:00)!* 🍽️\n\n"
                "Your work timer has been automatically paused for lunch.\n"
                "Enjoy your meal and take a breather! ☕🥪\n\n"
                "I will automatically resume your timer at *14:00*! 🚀"
            )
            await ctx.bot.send_message(
                chat_id=chat_id, text=msg,
                parse_mode="Markdown", reply_markup=kb_lunch_paused()
            )
            return

    # ── Auto Lunch Resume at 14:00 ────────────────────────────────
    elif sess["status"] == "paused":
        if sess.get("auto_lunch_paused") and not is_lunch_time(now):
            sess["auto_lunch_paused"] = False
            sess["lunch_override"] = False
            open_seg(sess, "work")
            sess["status"] = "working"
            save_data()
            s = calc(sess)
            msg = (
                "🔔 *14:00 — Lunch Break Ended!* ⏰\n\n"
                "Welcome back! Your work timer has automatically resumed.\n\n"
                f"⏱ Worked so far: *{fmt_dur(s['worked'])}* | ⏳ Remaining: *{fmt_dur(s['remaining'])}*\n\n"
                + pbar(s["pct"]) + "\n\n"
                "Let's make this afternoon productive! 💪"
            )
            await ctx.bot.send_message(
                chat_id=chat_id, text=msg,
                parse_mode="Markdown", reply_markup=kb_working()
            )
            return

    if sess["status"] != "working":
        return

    s = calc(sess)
    pct = s["pct"]

    # ── Goal reached ──────────────────────────────────────────
    if pct >= 100 and 100 not in sess["milestones_sent"]:
        sess["milestones_sent"].add(100)
        close_seg(sess)
        sess["status"] = "done"
        record_day_history(uid)
        save_data()
        ctx.job.schedule_removal()
        tl = timeline_text(sess)
        msg = r(GOAL_REACHED).format(
            goal=fmt_dur(s["goal"]), worked=fmt_dur(s["worked"]),
            rest=fmt_dur(s["resting"]), bar=pbar(100), timeline=tl
        )
        await ctx.bot.send_message(chat_id=chat_id, text=msg,
                                   parse_mode="Markdown", reply_markup=kb_done())
        return

    # ── Milestone messages ─────────────────────────────────────
    for threshold in (25, 50, 75):
        if pct >= threshold and threshold not in sess["milestones_sent"]:
            sess["milestones_sent"].add(threshold)
            save_data()
            s2 = calc(sess)
            text = (
                r(MILESTONE[threshold]) + "\n\n"
                f"⏱ Worked: *{fmt_dur(s2['worked'])}*  |  ⏳ Left: *{fmt_dur(s2['remaining'])}*\n\n"
                + pbar(s2["pct"])
            )
            await ctx.bot.send_message(chat_id=chat_id, text=text,
                                       parse_mode="Markdown", reply_markup=kb_working())

# ── Shared Actions ────────────────────────────────────────────
async def handle_begin(uid: int, chat_id: int, app, reply_fn):
    sess = get_sess(uid)
    sess["chat_id"] = chat_id
    if sess["status"] in ("working", "paused"):
        await reply_fn(r(ALREADY_WORKING), kb_for(sess))
        return

    now = now_local()
    sess["start_time"] = now
    sess["milestones_sent"] = set()

    # If it is lunch break (13:00 - 14:00), automatically set in pause mode!
    if is_lunch_time(now) and not sess.get("lunch_override", False):
        sess["status"] = "paused"
        sess["auto_lunch_paused"] = True
        sess["lunch_override"] = False
        open_seg(sess, "break")
        save_data()
        start_job(app, uid, chat_id)
        msg = (
            f"🍱 *Clocked in at {fmt_t(now)}, but it's Lunch Break (13:00 – 14:00)!*\n"
            f"🎯 Mission: *{fmt_dur(sess['goal_hours']*3600)}* of focused work!\n\n"
            "Your timer has been automatically set to *Pause* ⏸️ so lunch time isn't counted as work.\n\n"
            "It will automatically resume at *14:00*! Enjoy your meal! ☕🥪\n\n"
            "_(If you're working through lunch today, tap Work Anyway below)_"
        )
        await reply_fn(msg, kb_lunch_paused())
        return

    sess["status"] = "working"
    sess["auto_lunch_paused"] = False
    open_seg(sess, "work")
    save_data()
    start_job(app, uid, chat_id)
    await reply_fn(
        r(BEGIN).format(t=fmt_t(sess["start_time"]), goal=fmt_dur(sess["goal_hours"]*3600)),
        kb_working()
    )

async def handle_reset(uid: int, chat_id: int, app, reply_fn):
    stop_job(app, uid)
    sess = reset_session(uid, chat_id)
    goal_str = f"{sess['goal_hours']:g}h"
    msgs = [
        f"🔄 *All clear! Fresh slate!*\n🎯 Goal kept at: *{goal_str}*\n\nHit *▶️ Start Working* when you're ready to go! 🌅",
        f"✨ *Reset done!* Timeline cleared, goal kept at *{goal_str}*!\n\nPress *▶️ Start Working* to begin! 💪",
        f"🌱 *Starting fresh!* Tap *▶️ Start Working* when you're ready! 🚀",
    ]
    await reply_fn(r(msgs), kb_idle())

# ── Command handlers ──────────────────────────────────────────
async def cmd_start(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sess = get_sess(u.effective_user.id)
    sess["chat_id"] = u.effective_chat.id
    save_data()
    name = u.effective_user.first_name or "there"
    greeting = r(GREET).format(name=name)
    goal_str = f"{sess['goal_hours']:g} hours"
    await u.message.reply_text(
        f"{greeting}\n\n"
        f"I'm *WorkBot* — your personal daily hours tracker! Here's what I do:\n\n"
        f"⏱ Track work sessions with *pause & resume*\n"
        f"🍱 *Auto-pause for lunch break* from 13:00 to 14:00\n"
        f"📈 *Weekly & Monthly Reports* with 1-click buttons\n"
        f"📊 Show live progress with a full timeline\n"
        f"🎉 *Auto-notify you* the moment you hit your goal\n"
        f"🔥 Send milestone cheers at 25%, 50%, 75%\n\n"
        f"🎯 Current goal: *{goal_str}*  _(tap 🎯 Set Goal to change)_\n\n"
        f"Hit *▶️ Start Working* whenever you're ready! 💪",
        parse_mode="Markdown", reply_markup=kb_for(sess)
    )

async def cmd_begin(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async def reply(text, kb):
        await u.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    await handle_begin(u.effective_user.id, u.effective_chat.id, ctx.application, reply)

async def cmd_pause(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sess = get_sess(u.effective_user.id)
    if sess["status"] == "paused":
        await u.message.reply_text(r(ALREADY_PAUSED), parse_mode="Markdown", reply_markup=kb_for(sess))
        return
    if sess["status"] != "working":
        await u.message.reply_text(r(NOT_WORKING), parse_mode="Markdown", reply_markup=kb_for(sess))
        return
    open_seg(sess, "break")
    sess["status"] = "paused"
    if is_lunch_time():
        sess["auto_lunch_paused"] = True
    save_data()
    s = calc(sess)
    await u.message.reply_text(
        r(PAUSE).format(t=fmt_t(now_local()), worked=fmt_dur(s["worked"])),
        parse_mode="Markdown", reply_markup=kb_for(sess)
    )

async def cmd_resume(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    sess = get_sess(uid)
    if sess["status"] == "working":
        await u.message.reply_text(r(NOT_ON_BREAK), parse_mode="Markdown", reply_markup=kb_for(sess))
        return
    if sess["status"] != "paused":
        await u.message.reply_text(r(NOT_WORKING), parse_mode="Markdown", reply_markup=kb_for(sess))
        return
    if is_lunch_time():
        sess["lunch_override"] = True
    sess["auto_lunch_paused"] = False
    open_seg(sess, "work")
    sess["status"] = "working"
    save_data()
    s = calc(sess)
    start_job(ctx.application, uid, u.effective_chat.id)
    await u.message.reply_text(
        r(RESUME).format(t=fmt_t(now_local()), worked=fmt_dur(s["worked"]),
                         left=fmt_dur(s["remaining"]), bar=pbar(s["pct"])),
        parse_mode="Markdown", reply_markup=kb_working()
    )

async def cmd_status(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sess = get_sess(u.effective_user.id)
    if sess["status"] == "idle":
        await u.message.reply_text(r(NO_SESSION), parse_mode="Markdown", reply_markup=kb_idle())
        return
    text = status_card(sess) + "\n\n" + timeline_text(sess)
    await u.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_for(sess))

async def cmd_done(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    sess = get_sess(uid)
    if sess["status"] == "idle":
        await u.message.reply_text(r(NO_SESSION), parse_mode="Markdown", reply_markup=kb_idle())
        return
    stop_job(ctx.application, uid)
    close_seg(sess)
    sess["status"] = "done"
    record_day_history(uid)
    save_data()
    s = calc(sess)
    tl = timeline_text(sess)
    kw = dict(start=fmt_t(sess["start_time"]), end=fmt_t(now_local()),
              worked=fmt_dur(s["worked"]), rest=fmt_dur(s["resting"]),
              goal=fmt_dur(s["goal"]), bar=pbar(s["pct"]), timeline=tl)
    msg = r(DONE_GOAL).format(**kw) if s["remaining"] == 0 else r(DONE_PARTIAL).format(**kw)
    await u.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb_done())

async def cmd_reset(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async def reply(text, kb):
        await u.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    await handle_reset(u.effective_user.id, u.effective_chat.id, ctx.application, reply)

async def cmd_report(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    if ctx.args:
        arg = ctx.args[0].lower()
        if "week" in arg:
            await u.message.reply_text(get_weekly_report(uid), parse_mode="Markdown", reply_markup=kb_report_view("week"))
            return
        elif "month" in arg:
            await u.message.reply_text(get_monthly_report(uid), parse_mode="Markdown", reply_markup=kb_report_view("month"))
            return
    await u.message.reply_text(
        "📊 *Work Reports & Statistics*\n\n"
        "Choose a timeframe to view your progress:",
        parse_mode="Markdown",
        reply_markup=kb_reports_menu()
    )

# ── /setgoal conversation ─────────────────────────────────────
async def cmd_setgoal(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if ctx.args:
        return await _apply_goal(u, ctx, ctx.args[0])
    await u.message.reply_text(
        "🎯 *What's your working goal for today?*\n\nJust type a number — like `6`, `7.5`, or `8` hours:",
        parse_mode="Markdown"
    )
    return ASK_GOAL

async def goal_received(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    return await _apply_goal(u, ctx, u.message.text)

async def _apply_goal(u: Update, ctx: ContextTypes.DEFAULT_TYPE, raw: str) -> int:
    try:
        hours = float(raw.strip().replace("h", "").replace("hours", ""))
        assert 0 < hours <= 24
    except (ValueError, AssertionError):
        await u.message.reply_text(
            "🤔 Hmm, that doesn't look like a valid number.\nTry something like `6`, `7.5`, or `8` 😊",
            parse_mode="Markdown"
        )
        return ASK_GOAL
    uid = u.effective_user.id
    save_user_goal(uid, hours)
    sess = get_sess(uid)
    sess["goal_hours"] = hours
    save_data()
    msgs = [
        f"✅ *Goal set to {hours:g} hours!* Love the ambition! 🎯\n\nI'll celebrate every milestone and shout when you nail it! 🎉",
        f"🎯 *{hours:g} hours — let's go!* Goal locked in!\n\nI'll be cheering you on the whole way! 💪",
        f"✨ *Perfect!* {hours:g}-hour goal saved!\n\nExpect milestone updates and a big celebration when you finish! 🏆",
    ]
    await u.message.reply_text(r(msgs), parse_mode="Markdown", reply_markup=kb_for(sess))
    return ConversationHandler.END

async def cancel_conv(u: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await u.message.reply_text("No worries, cancelled! 👍 Let me know if you need anything.")
    return ConversationHandler.END

# ── Inline buttons ────────────────────────────────────────────
async def on_button(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = u.callback_query
    await q.answer()
    uid = u.effective_user.id
    chat_id = u.effective_chat.id
    sess = get_sess(uid)
    sess["chat_id"] = chat_id

    async def say(text: str, kb=None):
        await q.message.reply_text(text, parse_mode="Markdown", reply_markup=kb or kb_for(sess))

    async def edit(text: str, kb=None):
        try:
            await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb or kb_for(sess))
        except BadRequest:
            pass

    if q.data == "begin":
        await handle_begin(uid, chat_id, ctx.application, say)

    elif q.data == "pause":
        if sess["status"] == "paused":
            await say(r(ALREADY_PAUSED)); return
        if sess["status"] != "working":
            await say(r(NOT_WORKING)); return
        open_seg(sess, "break")
        sess["status"] = "paused"
        if is_lunch_time():
            sess["auto_lunch_paused"] = True
        save_data()
        s = calc(sess)
        await say(r(PAUSE).format(t=fmt_t(now_local()), worked=fmt_dur(s["worked"])), kb_for(sess))

    elif q.data == "resume":
        if sess["status"] == "working":
            await say(r(NOT_ON_BREAK)); return
        if sess["status"] != "paused":
            await say(r(NOT_WORKING)); return
        if is_lunch_time():
            sess["lunch_override"] = True
        sess["auto_lunch_paused"] = False
        open_seg(sess, "work")
        sess["status"] = "working"
        save_data()
        s = calc(sess)
        start_job(ctx.application, uid, chat_id)
        await say(r(RESUME).format(t=fmt_t(now_local()), worked=fmt_dur(s["worked"]),
                                   left=fmt_dur(s["remaining"]), bar=pbar(s["pct"])), kb_working())

    elif q.data == "lunch_override":
        sess["lunch_override"] = True
        sess["auto_lunch_paused"] = False
        open_seg(sess, "work")
        sess["status"] = "working"
        save_data()
        s = calc(sess)
        start_job(ctx.application, uid, chat_id)
        await say(
            "💼 *Work mode: ON!* Lunch break pause overridden.\n"
            f"🎯 Goal: *{fmt_dur(sess['goal_hours']*3600)}* today!\n\n"
            "I'm tracking your work — keep crushing it! 💪",
            kb_working()
        )

    elif q.data == "status":
        if sess["status"] == "idle":
            await say(r(NO_SESSION), kb_idle()); return
        await say(status_card(sess) + "\n\n" + timeline_text(sess))

    elif q.data == "done":
        if sess["status"] == "idle":
            await say(r(NO_SESSION), kb_idle()); return
        stop_job(ctx.application, uid)
        close_seg(sess)
        sess["status"] = "done"
        record_day_history(uid)
        save_data()
        s = calc(sess)
        tl = timeline_text(sess)
        kw = dict(start=fmt_t(sess["start_time"]), end=fmt_t(now_local()),
                  worked=fmt_dur(s["worked"]), rest=fmt_dur(s["resting"]),
                  goal=fmt_dur(s["goal"]), bar=pbar(s["pct"]), timeline=tl)
        msg = r(DONE_GOAL).format(**kw) if s["remaining"] == 0 else r(DONE_PARTIAL).format(**kw)
        await say(msg, kb_done())

    elif q.data == "setgoal":
        ctx.user_data["awaiting_goal"] = True
        await say("🎯 *What's your goal for today?*\n\nType a number like `6`, `7.5`, or `8`:",
                  InlineKeyboardMarkup([]))

    elif q.data == "reset":
        await handle_reset(uid, chat_id, ctx.application, say)

    # ── Report Callbacks ──────────────────────────────────────
    elif q.data == "reports_menu":
        await edit(
            "📊 *Work Reports & Statistics*\n\n"
            "Select a timeframe to view your progress:",
            kb_reports_menu()
        )

    elif q.data == "report_week":
        text = get_weekly_report(uid)
        await edit(text, kb_report_view("week"))

    elif q.data == "report_month":
        text = get_monthly_report(uid)
        await edit(text, kb_report_view("month"))

    elif q.data == "reports_back":
        if sess["status"] == "idle":
            await edit("👋 Hit *▶️ Start Working* whenever you're ready! 💪", kb_idle())
        elif sess["status"] == "done":
            await edit("🏁 Today's session is wrapped! Ready for a new day?", kb_done())
        else:
            await edit(status_card(sess) + "\n\n" + timeline_text(sess), kb_for(sess))

async def on_text(u: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("awaiting_goal"):
        ctx.user_data.pop("awaiting_goal")
        await _apply_goal(u, ctx, u.message.text)
        return
    sess = get_sess(u.effective_user.id)
    if sess["status"] == "idle":
        await u.message.reply_text(
            "👋 Hey! Ready to start tracking your day? Hit the button below! 😊",
            parse_mode="Markdown", reply_markup=kb_idle()
        )
    else:
        tips = [
            "💡 Need something? Try the buttons or /status, /pause, /resume, /done, /report",
            "😊 Use the buttons below to control your session, or type /status to check progress!",
            "👇 Everything you need is right in the buttons below!",
        ]
        await u.message.reply_text(r(tips), parse_mode="Markdown", reply_markup=kb_for(sess))

# ── App Startup ───────────────────────────────────────────────
async def post_init(app: Application):
    active_count = 0
    for uid, sess in sessions.items():
        if sess.get("status") in ("working", "paused") and sess.get("chat_id"):
            start_job(app, uid, sess["chat_id"])
            active_count += 1
    if active_count:
        logger.info("Restored tracking jobs for %d active user session(s)", active_count)

# ── Main ──────────────────────────────────────────────────────
def main():
    if not TOKEN:
        print("❌  BOT_TOKEN not set in .env"); return

    app = Application.builder().token(TOKEN).post_init(post_init).build()

    goal_conv = ConversationHandler(
        entry_points=[CommandHandler("setgoal", cmd_setgoal)],
        states={ASK_GOAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, goal_received)]},
        fallbacks=[CommandHandler("cancel", cancel_conv)],
    )

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("begin",   cmd_begin))
    app.add_handler(CommandHandler("pause",   cmd_pause))
    app.add_handler(CommandHandler("resume",  cmd_resume))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("report",  cmd_report))
    app.add_handler(goal_conv)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    print("🤖  WorkBot is running! Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
