"""
calculator.py — WorkBot Working-Time Calculation Engine
=======================================================

Pure, deterministic functions. No database calls, no side effects.

Architecture (Single Source of Truth)
--------------------------------------
Every metric — worked, lunch, other_break, remaining, pct, estimated_finish —
is derived from ONE canonical list of ``WorkInterval`` objects.

Steps
-----
1. Convert all raw break records to local-timezone-aware datetimes.
2. Build a *normalized* timeline of non-overlapping intervals, each classified as
   WORK | LUNCH | OTHER_BREAK.
3. Sum each category.  ``worked = SUM(WORK intervals)``.
4. Remaining  = max(goal - worked, 0)           — never recalculated separately.
5. Progress   = clamp(worked / goal * 100, 0-100).
6. Est. finish = now + remaining, skipping future mandatory-lunch windows.

Double-counting prevention
--------------------------
* Every second belongs to exactly one interval type.
* The mandatory lunch window (e.g. 13:00-14:00) is injected into the timeline
  as a LUNCH interval.  It is never *also* subtracted from a WORK interval.
* If the user is already on a manual break during the lunch window, the overlapping
  portion of that manual break is reclassified as LUNCH automatically so no second
  is counted twice.
* ``worked`` is ONLY ``SUM(WORK intervals)`` — no secondary subtractions anywhere.
"""

from __future__ import annotations

from datetime import datetime, date, time, timedelta
from enum import Enum
from zoneinfo import ZoneInfo
from typing import Optional
from dataclasses import dataclass, field


# ── Public data types ─────────────────────────────────────────────

class IntervalType(str, Enum):
    WORK        = "work"
    LUNCH       = "lunch"
    OTHER_BREAK = "other_break"


@dataclass
class BreakPeriod:
    """Raw break record from the database (tz-aware or naive-UTC)."""
    started_at: datetime
    ended_at: Optional[datetime]   # None = still open
    break_type: str                # "lunch" | "manual"


@dataclass
class WorkInterval:
    """One classified, non-overlapping segment of the normalized timeline."""
    start: datetime
    end: datetime
    interval_type: IntervalType

    @property
    def duration_secs(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class CalcResult:
    # Core metrics (all in seconds, exact floats — do not round before display)
    worked_secs: float          # Net working seconds  (SUM of WORK intervals)
    lunch_secs: float           # Lunch break seconds  (SUM of LUNCH intervals)
    other_break_secs: float     # Other break seconds  (SUM of OTHER_BREAK intervals)
    required_secs: float        # Goal seconds
    remaining_secs: float       # max(goal - worked, 0)
    pct: int                    # 0-100 progress
    projected_done_at: Optional[datetime]
    is_complete: bool

    # Normalized timeline — the single source of truth.
    # Both the status card and the timeline view read from here.
    intervals: list = field(default_factory=list)

    # Legacy aliases so existing bot code keeps working without changes
    @property
    def mandatory_secs(self) -> float:
        return self.lunch_secs

    @property
    def manual_secs(self) -> float:
        return self.other_break_secs


# ── Internal helpers ──────────────────────────────────────────────

def _to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz)


def _lunch_dt(day: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, t, tzinfo=tz)


def _build_normalized_timeline(
    session_start: datetime,
    session_end: datetime,
    breaks: list,          # list[BreakPeriod], already local tz
    lunch_start_t: time,
    lunch_end_t: time,
    tz: ZoneInfo,
) -> list:
    """
    Build a complete, non-overlapping, fully-classified timeline.

    Steps:
    1. Collect all raw break intervals, clamped to the session window.
    2. Inject the mandatory lunch window as a LUNCH slot.
    3. Resolve overlaps: LUNCH takes priority over OTHER_BREAK so that a
       manual break spanning the lunch window gets split — no second is
       classified twice.
    4. Fill gaps with WORK intervals.
    Returns sorted list of WorkInterval covering [session_start, session_end].
    """

    # --- 1. Raw break intervals (clamped) --------------------------------
    raw = []  # list of (start, end, IntervalType)

    for bp in breaks:
        b_start = bp.started_at
        b_end   = bp.ended_at if bp.ended_at is not None else session_end
        b_start = max(b_start, session_start)
        b_end   = min(b_end,   session_end)
        if b_end <= b_start:
            continue
        itype = IntervalType.LUNCH if bp.break_type == "lunch" else IntervalType.OTHER_BREAK
        raw.append((b_start, b_end, itype))

    # --- 2. Inject mandatory lunch window --------------------------------
    lunch_s = _lunch_dt(session_start.date(), lunch_start_t, tz)
    lunch_e = _lunch_dt(session_start.date(), lunch_end_t,   tz)
    lunch_s = max(lunch_s, session_start)
    lunch_e = min(lunch_e, session_end)
    if lunch_e > lunch_s:
        raw.append((lunch_s, lunch_e, IntervalType.LUNCH))

    # --- 3. Sort and resolve overlaps ------------------------------------
    # Sort: earlier start first; for same start, LUNCH before OTHER_BREAK
    raw.sort(key=lambda x: (x[0], 0 if x[2] == IntervalType.LUNCH else 1))

    events = []  # non-overlapping (start, end, IntervalType)
    for (s, e, itype) in raw:
        if not events:
            events.append((s, e, itype))
            continue
        prev_s, prev_e, prev_type = events[-1]
        if s >= prev_e:
            # No overlap
            events.append((s, e, itype))
        else:
            # Overlap: LUNCH wins over OTHER_BREAK
            if itype == IntervalType.LUNCH and prev_type != IntervalType.LUNCH:
                # New LUNCH takes priority; split previous interval
                events.pop()
                if s > prev_s:
                    events.append((prev_s, s, prev_type))
                overlap_end = min(e, prev_e)
                events.append((s, overlap_end, IntervalType.LUNCH))
                if overlap_end < prev_e:
                    events.append((overlap_end, prev_e, prev_type))
                if e > prev_e:
                    events.append((prev_e, e, IntervalType.LUNCH))
            else:
                # Previous has priority; trim new interval
                new_s = max(s, prev_e)
                if new_s < e:
                    events.append((new_s, e, itype))
                # else fully absorbed — discard

    # --- 4. Fill gaps with WORK -----------------------------------------
    intervals = []
    cursor = session_start
    for (s, e, itype) in events:
        if s > cursor:
            intervals.append(WorkInterval(cursor, s, IntervalType.WORK))
        intervals.append(WorkInterval(s, e, itype))
        cursor = e
    if cursor < session_end:
        intervals.append(WorkInterval(cursor, session_end, IntervalType.WORK))

    return intervals


# ── Public calculation function ───────────────────────────────────

def calculate(
    session_started_at: datetime,
    session_ended_at:   Optional[datetime],
    breaks:             list,
    required_hours:     float,
    scheduled_break_start: time,
    scheduled_break_end:   time,
    as_of:              Optional[datetime] = None,
    tz:                 ZoneInfo = None,
) -> CalcResult:
    """
    Compute working-time statistics from stored events.

    All metrics derive from the single canonical normalized timeline.
    No secondary subtraction of lunch time anywhere — if lunch is in the
    timeline (explicit break or injected mandatory window) it is excluded
    from WORK exactly once.
    """
    if tz is None:
        tz = ZoneInfo("Asia/Tashkent")

    session_started_at = _to_local(session_started_at, tz)
    if session_ended_at is not None:
        session_ended_at = _to_local(session_ended_at, tz)

    local_breaks = []
    for bp in breaks:
        local_breaks.append(BreakPeriod(
            started_at=_to_local(bp.started_at, tz),
            ended_at=_to_local(bp.ended_at, tz) if bp.ended_at is not None else None,
            break_type=bp.break_type,
        ))

    now = (as_of.astimezone(tz) if as_of else datetime.now(tz=tz))
    session_end = session_ended_at if session_ended_at is not None else now
    session_end = min(session_end, now)
    session_start = session_started_at

    has_open_break = any(bp.ended_at is None for bp in local_breaks)

    # ── Build the single canonical timeline ──────────────────────
    intervals = _build_normalized_timeline(
        session_start=session_start,
        session_end=session_end,
        breaks=local_breaks,
        lunch_start_t=scheduled_break_start,
        lunch_end_t=scheduled_break_end,
        tz=tz,
    )

    # ── Derive all metrics from intervals ─────────────────────────
    worked_secs      = sum(iv.duration_secs for iv in intervals if iv.interval_type == IntervalType.WORK)
    lunch_secs       = sum(iv.duration_secs for iv in intervals if iv.interval_type == IntervalType.LUNCH)
    other_break_secs = sum(iv.duration_secs for iv in intervals if iv.interval_type == IntervalType.OTHER_BREAK)

    required_secs  = required_hours * 3600.0
    remaining_secs = max(0.0, required_secs - worked_secs)
    pct = min(100, int(worked_secs / required_secs * 100)) if required_secs else 0
    is_complete = worked_secs >= required_secs

    projected_done_at: Optional[datetime] = None
    if not is_complete and session_ended_at is None:
        projected_done_at = _project_done_at(
            from_now=now,
            remaining_secs=remaining_secs,
            scheduled_break_start=scheduled_break_start,
            scheduled_break_end=scheduled_break_end,
            current_break_open=has_open_break,
        )

    return CalcResult(
        worked_secs=worked_secs,
        lunch_secs=lunch_secs,
        other_break_secs=other_break_secs,
        required_secs=required_secs,
        remaining_secs=remaining_secs,
        pct=pct,
        projected_done_at=projected_done_at,
        is_complete=is_complete,
        intervals=intervals,
    )


# ── Projected finish time ─────────────────────────────────────────

def _project_done_at(
    from_now: datetime,
    remaining_secs: float,
    scheduled_break_start: time,
    scheduled_break_end: time,
    current_break_open: bool = False,
) -> datetime:
    """
    Project the datetime when remaining_secs of actual work will complete,
    accounting for mandatory break windows that will be encountered.
    """
    cursor = from_now
    remaining = remaining_secs

    if current_break_open:
        tz = cursor.tzinfo
        break_end_today = datetime.combine(cursor.date(), scheduled_break_end, tzinfo=tz)
        if cursor < break_end_today:
            cursor = break_end_today

    while remaining > 0:
        tz = cursor.tzinfo
        lunch_start_dt = datetime.combine(cursor.date(), scheduled_break_start, tzinfo=tz)
        lunch_end_dt   = datetime.combine(cursor.date(), scheduled_break_end,   tzinfo=tz)

        if cursor < lunch_start_dt:
            work_until_lunch = (lunch_start_dt - cursor).total_seconds()
            if remaining <= work_until_lunch:
                return cursor + timedelta(seconds=remaining)
            remaining -= work_until_lunch
            cursor = lunch_end_dt
        elif cursor < lunch_end_dt:
            cursor = lunch_end_dt
        else:
            return cursor + timedelta(seconds=remaining)

        eod = datetime.combine(cursor.date(), time(23, 59, 59), tzinfo=tz)
        if cursor > eod:
            return cursor + timedelta(seconds=remaining)

    return cursor


# ── Formatting helpers ────────────────────────────────────────────

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
