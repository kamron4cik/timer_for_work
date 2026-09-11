# WorkBot 🤖 — Telegram Daily Hours Tracker

Track your daily working hours directly in Telegram with pause/resume and a full timeline log.

---

## ⚙️ Setup

### 1. Get a Bot Token
1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. *My WorkBot*) and a username (e.g. *myworkbot_bot*)
4. BotFather will give you a token like: `7123456789:AAFxxxxxx`

### 2. Create your `.env` file
```bash
cp .env.example .env
```
Open `.env` and replace the placeholder with your real token:
```
BOT_TOKEN=7123456789:AAFxxxxxx
```

### 3. Install dependencies
```bash
pip3 install -r requirements.txt
```

### 4. Run the bot
```bash
python3 bot.py
```

---

## 🎮 Commands

| Command | Description |
|---|---|
| `/start` | Welcome + show controls |
| `/setgoal [hours]` | Set daily goal (default 6h) |
| `/begin` | Start work session |
| `/pause` | Pause (start a break) |
| `/resume` | Resume working |
| `/status` | Show progress + timeline |
| `/done` | End session + final summary |
| `/reset` | Clear & start over |
| `/report` | Weekly & monthly work reports |

All commands are also available as **inline buttons** — no typing needed!

---

## 📅 Timeline Feature

Every pause and resume is logged as a separate entry. The `/status` command shows your full daily timeline:

```
💼 Work   09:00 → 10:30  (1h 30m)
☕ Break  10:30 → 11:00  (30m)
💼 Work   11:00 → 13:00  (2h 00m)
🍱 Break  13:00 → 14:00  (1h 00m)  (Lunch Break)
💼 Work   14:00 → 16:30  (2h 30m)  ← now
```

---

## 🍱 Automatic Lunch Break (13:00 – 14:00)
- The bot automatically pauses the timer at **13:00** for lunch break and notifies you.
- At **14:00**, it automatically resumes your work timer.
- If you work through lunch, a **▶️ Work Anyway** button lets you override the pause anytime.
- Custom daily goals (e.g. 4 hours) are permanently saved and preserved even when resetting the day.

---

## 🕐 Time Zone
The bot defaults to **Asia/Tashkent (UTC+5)**. To change it, edit this line in `bot.py`:
```python
LOCAL_TZ = ZoneInfo("Asia/Tashkent")
```
Replace with your zone, e.g. `"Europe/London"`, `"America/New_York"`, etc.
# timer_for_work
