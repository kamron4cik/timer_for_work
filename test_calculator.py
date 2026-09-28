"""
test_calculator.py — Unit tests for the WorkBot calculation engine.

Covers all 9 scenarios specified in the task requirements.
Run with:  python -m pytest test_calculator.py -v
"""

import pytest
from datetime import datetime, time, timedelta, date
from zoneinfo import ZoneInfo

from calculator import (
    calculate, BreakPeriod, CalcResult, IntervalType, WorkInterval,
    _build_normalized_timeline,
)

TZ = ZoneInfo("Asia/Tashkent")
LUNCH_START = time(13, 0)
LUNCH_END   = time(14, 0)
GOAL_HOURS  = 6.0


def dt(h: int, m: int = 0, s: int = 0, day: int = 1) -> datetime:
    """Build a tz-aware datetime on 2026-01-{day} at h:m:s local time."""
    return datetime(2026, 1, day, h, m, s, tzinfo=TZ)


def calc(
    started: datetime,
    ended: datetime | None = None,
    breaks: list[BreakPeriod] | None = None,
    goal: float = GOAL_HOURS,
    now: datetime | None = None,
) -> CalcResult:
    return calculate(
        session_started_at=started,
        session_ended_at=ended,
        breaks=breaks or [],
        required_hours=goal,
        scheduled_break_start=LUNCH_START,
        scheduled_break_end=LUNCH_END,
        as_of=now,
        tz=TZ,
    )


# ─────────────────────────────────────────────────────────────────
# Helper: convert seconds to (h, m) for readable assertions
# ─────────────────────────────────────────────────────────────────

def hm(secs: float) -> tuple[int, int]:
    secs = int(secs)
    return divmod(secs // 60, 60)


# ─────────────────────────────────────────────────────────────────
# Test 1 — Normal work, two sessions, no breaks except implicit lunch
# ─────────────────────────────────────────────────────────────────

def test_1_normal_work_goal_exactly_met():
    """
    09:00 → 13:00 WORK  (4h)
    13:00 → 14:00 LUNCH (automatic)
    14:00 → 16:00 WORK  (2h)

    Total WORK = 6h  →  remaining = 0
    """
    result = calc(
        started=dt(9),
        ended=dt(16),
        breaks=[],   # no manual breaks; lunch is injected automatically
        goal=6.0,
        now=dt(16),
    )
    assert hm(result.worked_secs) == (6, 0), f"Expected 6h 0m, got {hm(result.worked_secs)}"
    assert result.remaining_secs == 0.0
    assert result.is_complete is True
    assert result.pct == 100
    # Verify lunch is represented
    assert result.lunch_secs == 3600.0


# ─────────────────────────────────────────────────────────────────
# Test 2 — Long break, worked ≈ 5h56m, remaining ≈ 4m
# ─────────────────────────────────────────────────────────────────

def test_2_long_break_exact_values():
    """
    09:51 → 12:00  WORK  (2h09m = 7740s)
    12:00 → 15:22  BREAK (manual, 3h22m)
    15:22 → 19:09  WORK  (3h47m = 13620s)

    Total WORK = 2h09m + 3h47m = 5h56m = 21360s
    Remaining  = 6h00m - 5h56m = 4m = 240s
    """
    session_end = dt(19, 9)
    result = calc(
        started=dt(9, 51),
        ended=session_end,
        breaks=[BreakPeriod(dt(12, 0), dt(15, 22), "manual")],
        goal=6.0,
        now=session_end,
    )
    work_h, work_m = hm(result.worked_secs)
    assert work_h == 5, f"Expected 5h, got {work_h}h"
    assert work_m == 56, f"Expected 56m, got {work_m}m"
    assert result.remaining_secs < 300   # ≤ 5 minutes
    # No lunch interval should appear (break covered the whole window)
    lunch_in_timeline = [iv for iv in result.intervals if iv.interval_type == IntervalType.LUNCH]
    # Lunch window 13:00-14:00 is fully inside the manual break 12:00-15:22
    # so it gets reclassified but only the portion 13:00-14:00 should remain as LUNCH
    # within the OTHER_BREAK span; since 12:00-15:22 is OTHER_BREAK, the 13:00-14:00
    # sub-window is reclassified as LUNCH:
    assert len(lunch_in_timeline) == 1
    assert lunch_in_timeline[0].duration_secs == 3600.0


# ─────────────────────────────────────────────────────────────────
# Test 3 — Lunch inside working period; no other breaks
# ─────────────────────────────────────────────────────────────────

def test_3_lunch_inside_working_period():
    """
    09:00 → 18:00 elapsed, no manual breaks.
    Lunch 13:00-14:00 is injected automatically.

    WORK  = 9h00m - 1h00m lunch = 8h00m
    """
    result = calc(
        started=dt(9),
        ended=dt(18),
        breaks=[],
        goal=8.0,
        now=dt(18),
    )
    assert hm(result.worked_secs) == (8, 0), f"Expected 8h 0m, got {hm(result.worked_secs)}"
    assert result.lunch_secs == 3600.0
    assert result.other_break_secs == 0.0


# ─────────────────────────────────────────────────────────────────
# Test 4 — Lunch must NOT be double-counted
# ─────────────────────────────────────────────────────────────────

def test_4_lunch_not_double_counted_with_explicit_lunch_break():
    """
    Scenario: the DB contains an explicit 'lunch' break 13:00-14:00 AND the
    mandatory window is also 13:00-14:00.  The same hour must not be subtracted twice.

    09:00 → 18:00 with explicit lunch break 13:00-14:00 → WORK = 8h.
    """
    result = calc(
        started=dt(9),
        ended=dt(18),
        breaks=[BreakPeriod(dt(13), dt(14), "lunch")],
        goal=8.0,
        now=dt(18),
    )
    assert hm(result.worked_secs) == (8, 0), f"Expected 8h 0m, got {hm(result.worked_secs)}"
    assert result.lunch_secs == 3600.0
    assert result.other_break_secs == 0.0


def test_4b_manual_break_overlapping_lunch_no_double_count():
    """
    Manual break 12:00-15:22 overlaps lunch 13:00-14:00.
    The 13:00-14:00 portion is reclassified as LUNCH.
    Total break = 3h22m, WORK = 2h09m + 3h47m = 5h56m.
    """
    result = calc(
        started=dt(9, 51),
        ended=dt(19, 9),
        breaks=[BreakPeriod(dt(12, 0), dt(15, 22), "manual")],
        goal=6.0,
        now=dt(19, 9),
    )
    # Verify category totals add up to total elapsed
    total_elapsed = (dt(19, 9) - dt(9, 51)).total_seconds()
    total_classified = sum(iv.duration_secs for iv in result.intervals)
    assert abs(total_classified - total_elapsed) < 1, "Intervals must cover the full session"

    # Verify worked is consistent with remaining
    assert abs(result.worked_secs + result.remaining_secs - result.required_secs) < 1 or result.is_complete


# ─────────────────────────────────────────────────────────────────
# Test 5 — Currently working: open interval uses "now"
# ─────────────────────────────────────────────────────────────────

def test_5_currently_working_open_interval():
    """
    Session started 15:22, no breaks, now=17:00.
    Worked should be exactly 1h38m = 5880s.
    """
    started = dt(15, 22)
    now     = dt(17, 0)
    result  = calc(started=started, ended=None, breaks=[], goal=6.0, now=now)
    expected = (now - started).total_seconds()
    assert abs(result.worked_secs - expected) < 1, (
        f"Expected {expected}s worked, got {result.worked_secs}s"
    )


# ─────────────────────────────────────────────────────────────────
# Test 6 — Currently on break: worked must NOT increase
# ─────────────────────────────────────────────────────────────────

def test_6_on_break_worked_does_not_increase():
    """
    Started 09:00, took a break at 10:00 (open).
    Worked = exactly 1h, even when 'now' advances.
    """
    started   = dt(9)
    brk_start = dt(10)
    now1 = dt(11)  # 1h into break
    now2 = dt(12)  # 2h into break

    r1 = calc(started=started, ended=None,
               breaks=[BreakPeriod(brk_start, None, "manual")], goal=6.0, now=now1)
    r2 = calc(started=started, ended=None,
               breaks=[BreakPeriod(brk_start, None, "manual")], goal=6.0, now=now2)

    assert abs(r1.worked_secs - 3600) < 1, f"Expected 1h worked, got {r1.worked_secs}"
    assert abs(r2.worked_secs - 3600) < 1, f"Expected 1h worked, got {r2.worked_secs}"


# ─────────────────────────────────────────────────────────────────
# Test 7 — Estimated finish crosses mandatory lunch
# ─────────────────────────────────────────────────────────────────

def test_7_estimated_finish_crosses_lunch():
    """
    Current time: 12:50
    Started 10:00, worked 2h50m so far.
    Goal 6h → remaining = 3h10m.
    
    But a nearer scenario as specified in the task:
    remaining work = 30m, lunch at 13:00-14:00.
    10 min work → lunch → 20 min work → finish at 14:20.
    """
    # Set up: start 12:20, 30m of work done → remaining 5.5h. Too much for the scenario.
    # Use specified scenario directly: current=12:50, remaining=30m.
    from calculator import _project_done_at
    from datetime import time as dtime

    now = dt(12, 50)
    result_finish = _project_done_at(
        from_now=now,
        remaining_secs=30 * 60,          # 30 minutes
        scheduled_break_start=LUNCH_START,
        scheduled_break_end=LUNCH_END,
        current_break_open=False,
    )
    expected = dt(14, 20)  # 12:50 + 10m work + 60m lunch + 20m work
    diff = abs((result_finish - expected).total_seconds())
    assert diff < 60, f"Expected finish ~14:20, got {result_finish.strftime('%H:%M')}"


# ─────────────────────────────────────────────────────────────────
# Test 8 — Goal already completed
# ─────────────────────────────────────────────────────────────────

def test_8_goal_already_completed():
    """
    Worked 6h → remaining = 0, progress = 100%, is_complete = True.
    """
    result = calc(
        started=dt(9),
        ended=dt(16),   # 7h elapsed, minus 1h lunch = 6h worked
        breaks=[],
        goal=6.0,
        now=dt(16),
    )
    assert result.is_complete is True
    assert result.remaining_secs == 0.0
    assert result.pct == 100


def test_8b_overtime_is_clamped():
    """Working 9h (8h + 1h overtime); pct should not exceed 100."""
    result = calc(
        started=dt(9),
        ended=dt(18),   # 9h elapsed - 1h lunch = 8h worked
        breaks=[],
        goal=6.0,
        now=dt(18),
    )
    assert result.pct == 100
    assert result.is_complete is True
    assert result.remaining_secs == 0.0
    assert result.worked_secs > result.required_secs   # overtime


# ─────────────────────────────────────────────────────────────────
# Test 9 — Multiple breaks, no minute belongs to two categories
# ─────────────────────────────────────────────────────────────────

def test_9_multiple_breaks_no_overlap():
    """
    09:00 → 11:00  WORK
    11:00 → 11:20  BREAK (manual)
    11:20 → 13:00  WORK
    13:00 → 14:00  LUNCH (auto)
    14:00 → 16:00  WORK
    16:00 → 16:15  BREAK (manual)
    16:15 → 18:00  WORK  (now=18:00)

    Total elapsed = 9h.
    WORK   = 2h + 1h40m + 2h + 1h45m = 7h25m
    LUNCH  = 1h
    BREAK  = 20m + 15m = 35m
    Sum    = 7h25m + 1h + 35m = 9h  ✓
    """
    now = dt(18)
    result = calc(
        started=dt(9),
        ended=None,
        breaks=[
            BreakPeriod(dt(11, 0), dt(11, 20), "manual"),
            BreakPeriod(dt(16, 0), dt(16, 15), "manual"),
        ],
        goal=6.0,
        now=now,
    )

    total_elapsed = (now - dt(9)).total_seconds()
    total_classified = sum(iv.duration_secs for iv in result.intervals)

    assert abs(total_classified - total_elapsed) < 1, (
        f"Intervals do not cover full session: {total_classified}s vs {total_elapsed}s"
    )

    # Check each interval's type appears at most once contiguously (no overlaps)
    for i in range(len(result.intervals) - 1):
        assert result.intervals[i].end == result.intervals[i + 1].start, (
            f"Gap between intervals {i} and {i+1}"
        )

    # Lunch must be exactly 1h
    lunch_secs = sum(iv.duration_secs for iv in result.intervals
                     if iv.interval_type == IntervalType.LUNCH)
    assert lunch_secs == 3600.0

    # Other breaks must be 35m
    other_secs = sum(iv.duration_secs for iv in result.intervals
                     if iv.interval_type == IntervalType.OTHER_BREAK)
    assert abs(other_secs - 35 * 60) < 1, f"Expected 35m other breaks, got {other_secs/60:.1f}m"


# ─────────────────────────────────────────────────────────────────
# Additional — Validate the exact bug-report example
# ─────────────────────────────────────────────────────────────────

def test_bug_report_example():
    """
    The exact scenario from the bug report.
    
    Started: 09:51
    Break:   12:00 → 15:22  (manual)
    Now:     19:09 (approx)

    Expected (from task):
        worked ≈ 5h56m  (2h09m + 3h47m)
        remaining ≈ 4m
        progress ≈ 99%
    """
    now = dt(19, 9)
    result = calc(
        started=dt(9, 51),
        ended=None,
        breaks=[BreakPeriod(dt(12, 0), dt(15, 22), "manual")],
        goal=6.0,
        now=now,
    )

    # Worked should be roughly 5h56m
    work_h, work_m = hm(result.worked_secs)
    assert work_h == 5, f"Expected 5h worked, got {work_h}h {work_m}m"
    assert 54 <= work_m <= 58, f"Expected ~56m, got {work_m}m"

    # Remaining should be 2-6 minutes
    assert result.remaining_secs < 360, f"Remaining too large: {result.remaining_secs}s"

    # Progress should be ≥ 98%
    assert result.pct >= 98, f"Progress should be ~99%, got {result.pct}%"

    # All intervals must cover the full session without gaps
    total_elapsed = (now - dt(9, 51)).total_seconds()
    total_classified = sum(iv.duration_secs for iv in result.intervals)
    assert abs(total_classified - total_elapsed) < 1

    # Intervals must be contiguous
    for i in range(len(result.intervals) - 1):
        assert result.intervals[i].end == result.intervals[i + 1].start


# ─────────────────────────────────────────────────────────────────
# Additional — No break session (session entirely before lunch)
# ─────────────────────────────────────────────────────────────────

def test_no_break_before_lunch():
    """
    Session 09:00 → 12:00.  Goal 3h.
    No lunch overlap.  Worked = 3h.
    """
    result = calc(started=dt(9), ended=dt(12), breaks=[], goal=3.0, now=dt(12))
    assert hm(result.worked_secs) == (3, 0)
    assert result.lunch_secs == 0.0


# ─────────────────────────────────────────────────────────────────
# Additional — Consistency: worked + remaining = required (or worked >= required)
# ─────────────────────────────────────────────────────────────────

def test_consistency_worked_plus_remaining_equals_required():
    """
    For any combination, worked + remaining must always equal required
    (or worked >= required when done).
    """
    configs = [
        (dt(9), None,     [],                                      6.0, dt(11)),
        (dt(9), None,     [BreakPeriod(dt(10), None, "manual")],   6.0, dt(11)),
        (dt(9), dt(16),   [],                                      6.0, dt(16)),
        (dt(9, 51), None, [BreakPeriod(dt(12), dt(15, 22), "manual")], 6.0, dt(19, 9)),
    ]
    for started, ended, breaks, goal, now in configs:
        r = calc(started=started, ended=ended, breaks=breaks, goal=goal, now=now)
        if r.is_complete:
            assert r.remaining_secs == 0.0
            assert r.worked_secs >= r.required_secs
        else:
            diff = abs((r.worked_secs + r.remaining_secs) - r.required_secs)
            assert diff < 1, (
                f"worked({r.worked_secs:.0f}) + remaining({r.remaining_secs:.0f}) "
                f"!= required({r.required_secs:.0f})"
            )
