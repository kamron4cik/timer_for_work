"""
calculator.py — WorkBot Working-Time Calculation Engine

Pure, deterministic functions. No database calls, no side effects.
Takes stored events (session + breaks) and returns accurate working-time stats.

Key formula:
    worked = session_duration - mandatory_breaks_overlap - manual_breaks_duration
"""

from __future__ import annotations

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class BreakPeriod:
    started_at: datetime
    ended_at: Optional[datetime]   # None = still open
    break_type: str                # "lunch" | "manual"


@dataclass
class CalcResult:
    worked_secs: float         # Net working seconds
    mandatory_secs: float      # Mandatory (lunch) break seconds that occurred
    manual_secs: float         # Manual break seconds
    required_secs: float       # Required working seconds (from schedule)
    remaining_secs: float      # How many more seconds of work are needed
    pct: int                   # Completion percentage 0–100
    projected_done_at: Optional[datetime]  # When work will be complete
    is_complete: bool


def _overlap(
    seg_start: datetime,
    seg_end: datetime,
    period_start: datetime,
    period_end: datetime,
) -> float:
    """Return the overlap in seconds between two time intervals."""
    start = max(seg_start, period_start)
    end   = min(seg_end, period_end)
    return max(0.0, (end - start).total_seconds())


def _mandatory_break_overlap(
    work_start: datetime,
    work_end: datetime,
    break_start_t: time,
    break_end_t: time,
) -> float:
    """
    Calculate how many seconds of mandatory scheduled break fall within a work segment.
    The break is defined by clock times (e.g. 13:00–14:00) on the same calendar day.
    """
    # Build datetime objects on the same date as the work segment
    day = work_start.date()
    tz  = work_start.tzinfo

    b_start = datetime.combine(day, break_start_t, tzinfo=tz)
    b_end   = datetime.combine(day, break_end_t,   tzinfo=tz)

    # Handle edge case: work spans midnight — only consider same-day break
    return _overlap(work_start, work_end, b_start, b_end)


def calculate(
    session_started_at: datetime,          # UTC or tz-aware
    session_ended_at:   Optional[datetime],# None = still running
    breaks:             list[BreakPeriod],
    required_hours:     float,
    scheduled_break_start: time,           # e.g. time(13, 0)
    scheduled_break_end:   time,           # e.g. time(14, 0)
    as_of:              Optional[datetime] = None,  # Override "now"
    tz:                 ZoneInfo = None,
) -> CalcResult:
    """
    Compute working-time statistics from stored events.

    Algorithm:
    1. Determine the effective end of the session (ended_at or now).
    2. Walk through the session timeline in order.
    3. For each "work" gap (between breaks), subtract mandatory-break overlap.
    4. Sum all manual and lunch break durations.
    5. Return net worked, breaks, remaining, pct, projected_done_at.
    """
    if tz is None:
        tz = ZoneInfo("Asia/Tashkent")

    now = as_of or datetime.now(tz=session_started_at.tzinfo or tz)
    session_end = session_ended_at or now

    # Clamp to actual session boundaries
    session_end = min(session_end, now)

    if session_start := session_started_at:
        pass  # alias for clarity

    # Build a sorted list of break intervals within the session
    closed_breaks: list[tuple[datetime, datetime, str]] = []
    open_break_start: Optional[datetime] = None
    open_break_type: str = "manual"

    for bp in sorted(breaks, key=lambda b: b.started_at):
        b_start = bp.started_at
        b_end   = bp.ended_at or session_end  # open break ends at session end or now

        # Clamp to session
        b_start = max(b_start, session_start)
        b_end   = min(b_end, session_end)

        if b_end > b_start:
            closed_breaks.append((b_start, b_end, bp.break_type))
            if bp.ended_at is None:
                open_break_start = bp.started_at
                open_break_type  = bp.break_type

    # Sort breaks by start
    closed_breaks.sort(key=lambda x: x[0])

    # Build work segments (gaps between breaks)
    work_segs: list[tuple[datetime, datetime]] = []
    cursor = session_start

    for (b_start, b_end, _) in closed_breaks:
        if b_start > cursor:
            work_segs.append((cursor, b_start))
        cursor = max(cursor, b_end)

    # Final work segment after last break
    if cursor < session_end:
        work_segs.append((cursor, session_end))

    # Calculate worked time, subtracting mandatory break from each work segment
    worked_secs    = 0.0
    mandatory_secs = 0.0

    for (ws, we) in work_segs:
        seg_duration = (we - ws).total_seconds()
        # How much of the mandatory lunch break falls in this work segment?
        mb_overlap = _mandatory_break_overlap(ws, we, scheduled_break_start, scheduled_break_end)
        worked_secs    += max(0.0, seg_duration - mb_overlap)
        mandatory_secs += mb_overlap

    # Break durations
    manual_secs = sum(
        (b_end - b_start).total_seconds()
        for (b_start, b_end, btype) in closed_breaks
        if btype == "manual"
    )
    lunch_secs_explicit = sum(
        (b_end - b_start).total_seconds()
        for (b_start, b_end, btype) in closed_breaks
        if btype == "lunch"
    )
    # Mandatory break is the max of: explicitly tracked lunch breaks + implicit mandatory overlap
    mandatory_secs = max(mandatory_secs, lunch_secs_explicit)

    required_secs  = required_hours * 3600.0
    remaining_secs = max(0.0, required_secs - worked_secs)
    pct = min(100, int(worked_secs / required_secs * 100)) if required_secs else 0
    is_complete = worked_secs >= required_secs

    # Projected done time: when will remaining_secs of work accumulate from now?
    projected_done_at: Optional[datetime] = None
    if not is_complete and session_ended_at is None:
        projected_done_at = _project_done_at(
            from_now=now,
            remaining_secs=remaining_secs,
            scheduled_break_start=scheduled_break_start,
            scheduled_break_end=scheduled_break_end,
            current_break_open=(open_break_start is not None),
        )

    return CalcResult(
        worked_secs=worked_secs,
        mandatory_secs=mandatory_secs,
        manual_secs=manual_secs,
        required_secs=required_secs,
        remaining_secs=remaining_secs,
        pct=pct,
        projected_done_at=projected_done_at,
        is_complete=is_complete,
    )


def _project_done_at(
    from_now: datetime,
    remaining_secs: float,
    scheduled_break_start: time,
    scheduled_break_end: time,
    current_break_open: bool = False,
) -> datetime:
    """
    Project the datetime when `remaining_secs` of actual work will complete,
    accounting for any mandatory break windows that will be encountered.
    """
    cursor = from_now
    remaining = remaining_secs

    # If currently in a break, start accumulating from when break ends
    if current_break_open:
        tz = cursor.tzinfo
        break_end_today = datetime.combine(cursor.date(), scheduled_break_end, tzinfo=tz)
        if cursor < break_end_today:
            cursor = break_end_today

    while remaining > 0:
        tz = cursor.tzinfo
        # How far until mandatory break starts today?
        lunch_start_dt = datetime.combine(cursor.date(), scheduled_break_start, tzinfo=tz)
        lunch_end_dt   = datetime.combine(cursor.date(), scheduled_break_end,   tzinfo=tz)

        if cursor < lunch_start_dt:
            # Time until lunch
            work_until_lunch = (lunch_start_dt - cursor).total_seconds()
            if remaining <= work_until_lunch:
                return cursor + timedelta(seconds=remaining)
            # Consume work before lunch, then skip lunch
            remaining -= work_until_lunch
            cursor = lunch_end_dt
        elif cursor < lunch_end_dt:
            # Currently in lunch window (shouldn't normally reach here)
            cursor = lunch_end_dt
        else:
            # After lunch — no more breaks today
            return cursor + timedelta(seconds=remaining)

        # Safety: if we exceeded a full day, advance to next day
        # (handles late start + extremely long goal edge cases)
        eod = datetime.combine(cursor.date(), time(23, 59, 59), tzinfo=tz)
        if cursor > eod:
            cursor = datetime.combine(cursor.date() + timedelta(days=1), scheduled_break_start, tzinfo=tz)
            # Stop projecting across days — just return a rough estimate
            return cursor + timedelta(seconds=remaining)

    return cursor


# ── Formatting helpers ─────────────────────────────────────────

def fmt_dur(secs: float) -> str:
    """Format seconds into 'Xh YYm' or 'Ym ZZs'."""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_hm(secs: float) -> str:
    """Format seconds as 'H:MM' for export tables."""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m = rem // 60
    return f"{h}:{m:02d}"


def pbar(pct: int, w: int = 14) -> str:
    """ASCII progress bar in Telegram markdown."""
    f = int(w * pct / 100)
    return f"`{'█' * f}{'░' * (w - f)}` *{pct}%*"


def fmt_t(dt: Optional[datetime], tz: ZoneInfo = None) -> str:
    """Format datetime as HH:MM in local timezone."""
    if not dt:
        return "—"
    if tz:
        dt = dt.astimezone(tz)
    return dt.strftime("%H:%M")


def fmt_dt(dt: Optional[datetime], tz: ZoneInfo = None) -> str:
    """Format datetime as 'YYYY-MM-DD HH:MM' in local timezone."""
    if not dt:
        return "—"
    if tz:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M")
