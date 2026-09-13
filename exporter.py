"""
exporter.py — WorkBot CSV & Excel Export

Exports work history for a date range to CSV (UTF-8) or Excel (.xlsx).
"""

from __future__ import annotations

import io
import csv
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Optional

from sqlalchemy.orm import Session

from db import (
    User, WorkSession, WorkSchedule,
    get_sessions_for_range, get_schedule_for_weekday,
)
from calculator import (
    calculate, BreakPeriod, fmt_dur, fmt_hm, fmt_t,
)


COLUMNS = [
    "Date", "Day", "Sched Start", "Sched End",
    "Actual Start", "Actual End",
    "Required Work", "Actual Work",
    "Mandatory Break", "Manual Break",
    "Overtime", "Status", "Session Type", "Notes",
]

WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
STATUS_LABELS = {
    "active": "In Progress",
    "paused": "Paused",
    "lunch":  "Lunch Break",
    "done":   "Completed",
    "extra":  "Extra Work",
}


def _build_rows(
    db: Session,
    user: User,
    start: date,
    end: date,
) -> list[dict]:
    tz = ZoneInfo(user.timezone)
    sessions = get_sessions_for_range(db, user.id, start, end)

    # Group sessions by date (there can theoretically be multiple per day)
    from collections import defaultdict
    by_date: dict[date, list[WorkSession]] = defaultdict(list)
    for sess in sessions:
        by_date[sess.date].append(sess)

    rows = []
    d = start
    while d <= end:
        sched: Optional[WorkSchedule] = get_schedule_for_weekday(db, user.id, d.weekday())
        day_sessions = by_date.get(d, [])

        if not day_sessions:
            # No session recorded for this day
            if sched and sched.is_working_day:
                rows.append(_empty_row(d, sched, tz, status="No Session"))
        else:
            for sess in day_sessions:
                rows.append(_session_row(sess, sched, tz))

        from datetime import timedelta
        d = d + timedelta(days=1)

    return rows


def _empty_row(d: date, sched: Optional[WorkSchedule], tz: ZoneInfo, status: str = "") -> dict:
    sched_start = sched.start_time.strftime("%H:%M") if sched and sched.start_time else "—"
    sched_end   = sched.end_time.strftime("%H:%M")   if sched and sched.end_time   else "—"
    req         = fmt_hm(sched.required_hours * 3600) if sched and sched.required_hours else "—"
    return {
        "Date": d.strftime("%Y-%m-%d"),
        "Day": WEEKDAY_NAMES[d.weekday()],
        "Sched Start": sched_start,
        "Sched End": sched_end,
        "Actual Start": "—",
        "Actual End": "—",
        "Required Work": req,
        "Actual Work": "—",
        "Mandatory Break": "—",
        "Manual Break": "—",
        "Overtime": "—",
        "Status": status,
        "Session Type": "—",
        "Notes": "",
    }


def _session_row(sess: WorkSession, sched: Optional[WorkSchedule], tz: ZoneInfo) -> dict:
    from datetime import time as dtime
    from config import DEFAULT_LUNCH_START, DEFAULT_LUNCH_END

    lunch_s_h, lunch_s_m = map(int, DEFAULT_LUNCH_START.split(":"))
    lunch_e_h, lunch_e_m = map(int, DEFAULT_LUNCH_END.split(":"))
    sched_lunch_start = dtime(lunch_s_h, lunch_s_m)
    sched_lunch_end   = dtime(lunch_e_h, lunch_e_m)

    required_hours = (sched.required_hours if sched and sched.required_hours else 8.0)

    break_periods = [
        BreakPeriod(
            started_at=b.started_at,
            ended_at=b.ended_at,
            break_type=b.break_type,
        )
        for b in sess.breaks
    ]

    result = calculate(
        session_started_at=sess.started_at,
        session_ended_at=sess.ended_at,
        breaks=break_periods,
        required_hours=required_hours,
        scheduled_break_start=sched_lunch_start,
        scheduled_break_end=sched_lunch_end,
        tz=tz,
    )

    overtime = max(0.0, result.worked_secs - result.required_secs)
    actual_start = sess.started_at.astimezone(tz).strftime("%H:%M")
    actual_end   = sess.ended_at.astimezone(tz).strftime("%H:%M") if sess.ended_at else "—"
    sched_start  = sched.start_time.strftime("%H:%M") if sched and sched.start_time else "—"
    sched_end    = sched.end_time.strftime("%H:%M")   if sched and sched.end_time   else "—"

    return {
        "Date": sess.date.strftime("%Y-%m-%d"),
        "Day": WEEKDAY_NAMES[sess.date.weekday()],
        "Sched Start": sched_start,
        "Sched End": sched_end,
        "Actual Start": actual_start,
        "Actual End": actual_end,
        "Required Work": fmt_hm(result.required_secs),
        "Actual Work": fmt_hm(result.worked_secs),
        "Mandatory Break": fmt_hm(result.mandatory_secs),
        "Manual Break": fmt_hm(result.manual_secs),
        "Overtime": fmt_hm(overtime) if overtime > 0 else "0:00",
        "Status": STATUS_LABELS.get(sess.status, sess.status),
        "Session Type": sess.session_type,
        "Notes": "",
    }


# ── CSV Export ─────────────────────────────────────────────────

def export_csv(db: Session, user: User, start: date, end: date) -> bytes:
    rows = _build_rows(db, user, start, end)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")   # UTF-8 with BOM for Excel compatibility


# ── Excel Export ───────────────────────────────────────────────

def export_excel(db: Session, user: User, start: date, end: date) -> bytes:
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise RuntimeError("openpyxl is not installed. Run: pip install openpyxl")

    rows = _build_rows(db, user, start, end)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Work History"

    # Header style
    header_fill = PatternFill("solid", fgColor="1E3A5F")
    header_font = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    thin_border = Border(
        left=Side(style="thin", color="CCCCCC"),
        right=Side(style="thin", color="CCCCCC"),
        top=Side(style="thin", color="CCCCCC"),
        bottom=Side(style="thin", color="CCCCCC"),
    )

    # Alternating row fills
    row_fill_even = PatternFill("solid", fgColor="F0F4FA")
    row_fill_odd  = PatternFill("solid", fgColor="FFFFFF")

    # Status color map
    status_colors = {
        "Completed":   "D4EDDA",
        "In Progress": "FFF3CD",
        "No Session":  "F8D7DA",
        "Extra Work":  "E2D9F3",
    }

    # Write headers
    ws.row_dimensions[1].height = 36
    for col_idx, col_name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font    = header_font
        cell.fill    = header_fill
        cell.alignment = header_align
        cell.border  = thin_border

    # Write data rows
    for row_idx, row_data in enumerate(rows, start=2):
        fill = row_fill_even if row_idx % 2 == 0 else row_fill_odd

        # Override fill based on status
        status = row_data.get("Status", "")
        for key, color in status_colors.items():
            if key in status:
                fill = PatternFill("solid", fgColor=color)
                break

        for col_idx, col_name in enumerate(COLUMNS, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=row_data.get(col_name, ""))
            cell.fill      = fill
            cell.border    = thin_border
            cell.alignment = Alignment(horizontal="center", vertical="center")
            cell.font      = Font(name="Calibri", size=10)

    # Auto-fit column widths
    col_widths = {
        "Date": 12, "Day": 12, "Sched Start": 12, "Sched End": 12,
        "Actual Start": 13, "Actual End": 13,
        "Required Work": 14, "Actual Work": 12,
        "Mandatory Break": 16, "Manual Break": 13,
        "Overtime": 10, "Status": 14, "Session Type": 14, "Notes": 20,
    }
    for col_idx, col_name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = col_widths.get(col_name, 14)

    # Freeze header row
    ws.freeze_panes = "A2"

    # Add auto-filter
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(rows) + 1}"

    # Summary sheet
    ws_sum = wb.create_sheet("Summary")
    ws_sum.title = "Summary"
    total_worked   = sum(0.0 for _ in rows)   # placeholder — filled below
    completed_days = 0
    total_worked_s = 0.0

    for row in rows:
        aw = row.get("Actual Work", "—")
        if aw not in ("—", ""):
            try:
                h, m = map(int, aw.split(":"))
                total_worked_s += h * 3600 + m * 60
                completed_days += 1
            except Exception:
                pass

    summary_data = [
        ("Period", f"{start.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"),
        ("Total Days", str(len(rows))),
        ("Days Worked", str(completed_days)),
        ("Total Worked", fmt_hm(total_worked_s)),
        ("Average / Day", fmt_hm(total_worked_s / completed_days) if completed_days else "—"),
    ]
    ws_sum.column_dimensions["A"].width = 20
    ws_sum.column_dimensions["B"].width = 28
    for r_idx, (label, value) in enumerate(summary_data, start=1):
        ws_sum.cell(row=r_idx, column=1, value=label).font = Font(bold=True, name="Calibri", size=11)
        ws_sum.cell(row=r_idx, column=2, value=value).font = Font(name="Calibri", size=11)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
