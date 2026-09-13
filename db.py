"""
db.py — WorkBot Database Layer

SQLAlchemy ORM models + session/query helpers.
All timestamps stored as UTC in the database.
All display is done in the user's configured timezone.
"""

from __future__ import annotations

import logging
from datetime import datetime, date, time
from typing import Optional

from sqlalchemy import (
    create_engine, Integer, String, Float, Boolean,
    DateTime, Date, Time, ForeignKey, UniqueConstraint, Index, text,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship, Session, sessionmaker,
)

from config import DATABASE_URL

logger = logging.getLogger(__name__)

# ── Engine ─────────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

# Enable WAL mode for better concurrency
with engine.connect() as _conn:
    _conn.execute(text("PRAGMA journal_mode=WAL"))
    _conn.execute(text("PRAGMA foreign_keys=ON"))
    _conn.commit()

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_db() -> Session:
    return SessionLocal()


# ── ORM Models ─────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id:          Mapped[int]      = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int]      = mapped_column(Integer, unique=True, nullable=False)
    timezone:    Mapped[str]      = mapped_column(String(64), default="Asia/Tashkent")
    goal_hours:  Mapped[float]    = mapped_column(Float, default=8.0)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    schedules:   Mapped[list["WorkSchedule"]]  = relationship("WorkSchedule",  back_populates="user", cascade="all, delete-orphan")
    breaks_cfg:  Mapped[list["BreakSchedule"]] = relationship("BreakSchedule", back_populates="user", cascade="all, delete-orphan")
    sessions:    Mapped[list["WorkSession"]]   = relationship("WorkSession",   back_populates="user", cascade="all, delete-orphan")
    holidays:    Mapped[list["Holiday"]]       = relationship("Holiday",       back_populates="user", cascade="all, delete-orphan")
    notifications: Mapped[list["NotificationLog"]] = relationship("NotificationLog", back_populates="user", cascade="all, delete-orphan")


class WorkSchedule(Base):
    """One row per weekday per user. weekday: 0=Mon, 6=Sun."""
    __tablename__ = "work_schedules"
    __table_args__ = (UniqueConstraint("user_id", "weekday"),)

    id:             Mapped[int]   = mapped_column(Integer, primary_key=True)
    user_id:        Mapped[int]   = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    weekday:        Mapped[int]   = mapped_column(Integer, nullable=False)   # 0=Mon…6=Sun
    is_working_day: Mapped[bool]  = mapped_column(Boolean, default=True)
    start_time:     Mapped[Optional[time]] = mapped_column(Time)
    end_time:       Mapped[Optional[time]] = mapped_column(Time)
    required_hours: Mapped[Optional[float]] = mapped_column(Float)

    user: Mapped["User"] = relationship("User", back_populates="schedules")


class BreakSchedule(Base):
    """Configured automatic break periods (e.g. mandatory lunch 13:00–14:00)."""
    __tablename__ = "break_schedules"

    id:         Mapped[int]  = mapped_column(Integer, primary_key=True)
    user_id:    Mapped[int]  = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time:   Mapped[time] = mapped_column(Time, nullable=False)
    break_type: Mapped[str]  = mapped_column(String(32), default="mandatory")  # mandatory | custom

    user: Mapped["User"] = relationship("User", back_populates="breaks_cfg")


class WorkSession(Base):
    __tablename__ = "work_sessions"
    __table_args__ = (Index("ix_work_sessions_user_date", "user_id", "date"),)

    id:           Mapped[int]            = mapped_column(Integer, primary_key=True)
    user_id:      Mapped[int]            = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    date:         Mapped[date]           = mapped_column(Date, nullable=False)
    started_at:   Mapped[datetime]       = mapped_column(DateTime, nullable=False)  # UTC
    ended_at:     Mapped[Optional[datetime]] = mapped_column(DateTime)              # UTC; None = open
    status:       Mapped[str]            = mapped_column(String(32), default="active")
    # active | paused | lunch | done | extra
    session_type: Mapped[str]            = mapped_column(String(32), default="normal")
    # normal | extra (worked on non-working day)
    chat_id:      Mapped[Optional[int]]  = mapped_column(Integer)

    user:   Mapped["User"]         = relationship("User", back_populates="sessions")
    breaks: Mapped[list["Break"]]  = relationship("Break", back_populates="session", cascade="all, delete-orphan")


class Break(Base):
    __tablename__ = "breaks"

    id:         Mapped[int]              = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int]              = mapped_column(Integer, ForeignKey("work_sessions.id"), nullable=False)
    started_at: Mapped[datetime]         = mapped_column(DateTime, nullable=False)  # UTC
    ended_at:   Mapped[Optional[datetime]] = mapped_column(DateTime)                # None = open
    break_type: Mapped[str]              = mapped_column(String(32), default="manual")
    # manual | lunch

    session: Mapped["WorkSession"] = relationship("WorkSession", back_populates="breaks")


class Holiday(Base):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("user_id", "date"),)

    id:      Mapped[int]  = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int]  = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    date:    Mapped[date] = mapped_column(Date, nullable=False)
    reason:  Mapped[Optional[str]] = mapped_column(String(256))

    user: Mapped["User"] = relationship("User", back_populates="holidays")


class NotificationLog(Base):
    """Idempotency guard — prevents duplicate notifications on restart."""
    __tablename__ = "notification_logs"
    __table_args__ = (UniqueConstraint("user_id", "notif_type", "date"),)

    id:           Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id:      Mapped[int]      = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    notif_type:   Mapped[str]      = mapped_column(String(64))
    # morning_reminder | lunch_start | lunch_end | goal_reached | eod_reminder
    date:         Mapped[date]     = mapped_column(Date, nullable=False)
    sent_at:      Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship("User", back_populates="notifications")


# ── Create tables ───────────────────────────────────────────────
def init_db():
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables created/verified.")


# ── User helpers ────────────────────────────────────────────────
def get_or_create_user(db: Session, telegram_id: int) -> User:
    from config import DEFAULT_TIMEZONE, DEFAULT_REQUIRED_HOURS, DEFAULT_WORK_DAYS
    from config import DEFAULT_WORK_START, DEFAULT_WORK_END, DEFAULT_LUNCH_START, DEFAULT_LUNCH_END
    from datetime import time as dtime

    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user:
        return user

    user = User(
        telegram_id=telegram_id,
        timezone=DEFAULT_TIMEZONE,
        goal_hours=DEFAULT_REQUIRED_HOURS,
    )
    db.add(user)
    db.flush()   # get user.id

    # Create default weekly schedule
    start_h, start_m = map(int, DEFAULT_WORK_START.split(":"))
    end_h, end_m     = map(int, DEFAULT_WORK_END.split(":"))
    lunch_s_h, lunch_s_m = map(int, DEFAULT_LUNCH_START.split(":"))
    lunch_e_h, lunch_e_m = map(int, DEFAULT_LUNCH_END.split(":"))

    for wd in range(7):
        is_work = wd in DEFAULT_WORK_DAYS
        sched = WorkSchedule(
            user_id=user.id,
            weekday=wd,
            is_working_day=is_work,
            start_time=dtime(start_h, start_m) if is_work else None,
            end_time=dtime(end_h, end_m) if is_work else None,
            required_hours=DEFAULT_REQUIRED_HOURS if is_work else None,
        )
        db.add(sched)

    # Default mandatory lunch break
    lunch_break = BreakSchedule(
        user_id=user.id,
        start_time=dtime(lunch_s_h, lunch_s_m),
        end_time=dtime(lunch_e_h, lunch_e_m),
        break_type="mandatory",
    )
    db.add(lunch_break)

    db.commit()
    logger.info("Created new user telegram_id=%d", telegram_id)
    return user


def get_active_session(db: Session, user_id: int, for_date: date) -> Optional[WorkSession]:
    return (
        db.query(WorkSession)
        .filter(
            WorkSession.user_id == user_id,
            WorkSession.date == for_date,
            WorkSession.status != "done",
        )
        .order_by(WorkSession.started_at.desc())
        .first()
    )


def get_open_break(db: Session, session_id: int) -> Optional[Break]:
    return (
        db.query(Break)
        .filter(Break.session_id == session_id, Break.ended_at == None)  # noqa: E711
        .first()
    )


def get_schedule_for_weekday(db: Session, user_id: int, weekday: int) -> Optional[WorkSchedule]:
    return (
        db.query(WorkSchedule)
        .filter(WorkSchedule.user_id == user_id, WorkSchedule.weekday == weekday)
        .first()
    )


def is_holiday(db: Session, user_id: int, d: date) -> Optional[Holiday]:
    return (
        db.query(Holiday)
        .filter(Holiday.user_id == user_id, Holiday.date == d)
        .first()
    )


def notification_sent_today(db: Session, user_id: int, notif_type: str, d: date) -> bool:
    return (
        db.query(NotificationLog)
        .filter(
            NotificationLog.user_id == user_id,
            NotificationLog.notif_type == notif_type,
            NotificationLog.date == d,
        )
        .first()
    ) is not None


def mark_notification_sent(db: Session, user_id: int, notif_type: str, d: date):
    log = NotificationLog(user_id=user_id, notif_type=notif_type, date=d)
    try:
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()  # Duplicate = already sent, that's fine


def get_sessions_for_range(db: Session, user_id: int, start: date, end: date) -> list[WorkSession]:
    return (
        db.query(WorkSession)
        .filter(
            WorkSession.user_id == user_id,
            WorkSession.date >= start,
            WorkSession.date <= end,
        )
        .order_by(WorkSession.date, WorkSession.started_at)
        .all()
    )


def get_all_active_sessions(db: Session) -> list[WorkSession]:
    """Used on startup to restore scheduler jobs for all users."""
    return (
        db.query(WorkSession)
        .filter(WorkSession.status.in_(["active", "paused", "lunch"]))
        .all()
    )
