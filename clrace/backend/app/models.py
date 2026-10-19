"""CL Race 2026 — SQLModel data model.

Mirrors the conceptual schema in build-guide §14. Uses SQLite via SQLModel.
Explicit IDs / FKs; relationships resolved in application code (keeps ORM simple
and auditable for a small event).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts_iso(v: datetime | None) -> str | None:
    return v.isoformat() if v is not None else None


# ---------- Auth / identity ----------
class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    password_hash: str
    display_name: str
    role: str = Field(index=True)  # admin | team_leader
    team_id: Optional[int] = Field(default=None, foreign_key="teams.id", index=True)
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Session(SQLModel, table=True):
    """HttpOnly cookie sessions (revocable server-side)."""
    __tablename__ = "sessions"
    token: str = Field(primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None


# ---------- Event ----------
class Event(SQLModel, table=True):
    __tablename__ = "events"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    tagline: str = ""
    status: str = Field(default="not_started", index=True)  # not_started | live | paused | completed
    active_event_mode_id: Optional[int] = Field(default=None, foreign_key="event_modes.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class EventMode(SQLModel, table=True):
    __tablename__ = "event_modes"
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id", index=True)
    name: str  # Dry Weather | Wet Weather
    active: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


# ---------- Teams ----------
class Team(SQLModel, table=True):
    __tablename__ = "teams"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    route_id: Optional[int] = Field(default=None, foreign_key="routes.id", index=True)
    release_order: int = Field(default=0)
    status: str = Field(default="ready", index=True)  # ready | racing | finished | paused | disabled
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    final_elapsed_seconds: Optional[int] = None
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------- Checkpoints & routes ----------
class Checkpoint(SQLModel, table=True):
    __tablename__ = "checkpoints"
    id: Optional[int] = Field(default=None, primary_key=True)
    event_mode_id: int = Field(foreign_key="event_modes.id", index=True)
    checkpoint_number: int  # 1..7 (dry) / 1..9 (wet)
    location: str
    category: str = ""
    mission_title: str
    mission_description: str
    requirements: str = ""          # newline-separated proof requirements
    default_score: int = Field(default=10)
    bonus_description: str = ""
    is_final: bool = Field(default=False)
    active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)


class CheckpointImage(SQLModel, table=True):
    __tablename__ = "checkpoint_images"
    id: Optional[int] = Field(default=None, primary_key=True)
    checkpoint_id: int = Field(foreign_key="checkpoints.id", index=True)
    image_url: str
    thumbnail_url: str = ""
    sort_order: int = Field(default=0)


class Route(SQLModel, table=True):
    __tablename__ = "routes"
    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id", index=True)
    name: str  # Forward | Reverse | ...
    active: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


class RouteCheckpoint(SQLModel, table=True):
    __tablename__ = "route_checkpoints"
    __table_args__ = (UniqueConstraint("route_id", "checkpoint_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    route_id: int = Field(foreign_key="routes.id", index=True)
    checkpoint_id: int = Field(foreign_key="checkpoints.id", index=True)
    sequence_number: int


# ---------- Team progress ----------
class TeamProgress(SQLModel, table=True):
    __tablename__ = "team_progress"
    __table_args__ = (UniqueConstraint("team_id", "checkpoint_id"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="teams.id", index=True)
    checkpoint_id: int = Field(foreign_key="checkpoints.id", index=True)
    sequence_position: int = Field(default=1)
    status: str = Field(default="locked", index=True)  # locked|available|pending|rejected|completed
    unlocked_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


# ---------- Submissions ----------
class Submission(SQLModel, table=True):
    __tablename__ = "submissions"
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="teams.id", index=True)
    checkpoint_id: int = Field(foreign_key="checkpoints.id", index=True)
    photo_url: str
    thumbnail_url: str = ""
    attempt_number: int = Field(default=1)
    status: str = Field(default="pending", index=True)  # pending|rejected|completed
    submitted_at: datetime = Field(default_factory=utcnow, index=True)
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[int] = Field(default=None, foreign_key="users.id")
    admin_comment: str = ""
    base_score: int = Field(default=10)
    bonus_score: int = Field(default=0)
    total_score: int = Field(default=10)
    is_replaced: bool = Field(default=False)  # replaced before review (audit)


class ScoreAdjustment(SQLModel, table=True):
    __tablename__ = "score_adjustments"
    id: Optional[int] = Field(default=None, primary_key=True)
    team_id: int = Field(foreign_key="teams.id", index=True)
    adjustment_type: str  # time_bonus | completion_bonus | manual_bonus | penalty | correction
    points: int
    reason: str
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=utcnow)


# ---------- Audit ----------
class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_logs"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    action: str = Field(index=True)
    entity_type: str = ""
    entity_id: Optional[int] = None
    before_json: str = ""
    after_json: str = ""
    ip_address: str = ""
    created_at: datetime = Field(default_factory=utcnow, index=True)


# ---------- Settings ----------
class Setting(SQLModel, table=True):
    __tablename__ = "settings"
    key: str = Field(primary_key=True)
    value: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


# ------------------------------------------------------------------- hall of fame
class HallOfFame(SQLModel, table=True):
    """Admin-curated showcase: highlighted photos with an optional rank 1-20."""
    __tablename__ = "hall_of_fame"

    id: Optional[int] = Field(default=None, primary_key=True)
    submission_id: int = Field(foreign_key="submissions.id", index=True, unique=True)
    rank: Optional[int] = None
    headline: str = ""
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
