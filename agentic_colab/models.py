"""Database tables.

`events` is an append-only history log. The other tables hold current state.
All timestamps are naive UTC datetimes (SQLite has no timezone support); they
are serialized with a trailing "Z".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

ACTOR_TYPES = ("human", "agent", "system")

MESSAGE_TYPES = ("status", "proposal", "question", "decision", "human_input", "control", "finding", "review")
CHANNELS = ("general", "decisions", "findings", "compute", "system", "activity", "ask", "literature")

TASK_STATUSES = ("open", "claimed", "in_progress", "needs_input", "paused", "completed", "failed")
# Statuses in which a task is held by an agent under a lease.
HELD_STATUSES = ("claimed", "in_progress", "needs_input", "paused")
TERMINAL_STATUSES = ("completed", "failed")

# Statuses an agent can report about itself. "paused" is set by humans and
# "offline" is derived from the heartbeat age, so neither is reportable.
AGENT_REPORTABLE_STATUSES = ("idle", "working", "blocked", "waiting_on_human")
AGENT_STATUSES = AGENT_REPORTABLE_STATUSES + ("paused", "offline")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") + "Z" if dt else None


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_name: Mapped[str] = mapped_column(String(100))
    actor_type: Mapped[str] = mapped_column(String(10))
    type: Mapped[str] = mapped_column(String(50), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": iso(self.ts),
            "actor_name": self.actor_name,
            "actor_type": self.actor_type,
            "type": self.type,
            "payload": self.payload,
        }


class CharterVersion(Base):
    __tablename__ = "charter_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    objective: Mapped[str] = mapped_column(Text, default="")
    constraints: Mapped[str] = mapped_column(Text, default="")
    libraries: Mapped[str] = mapped_column(Text, default="")
    compute: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    edited_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    FIELDS = ("objective", "constraints", "libraries", "compute", "notes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            **{f: getattr(self, f) for f in self.FIELDS},
            "edited_by": self.edited_by,
            "created_at": iso(self.created_at),
        }


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    owner_name: Mapped[str] = mapped_column(String(100), default="")
    machine: Mapped[str] = mapped_column(String(200), default="")
    skills: Mapped[list[str]] = mapped_column(JSON, default=list)
    current_task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    approach: Mapped[str] = mapped_column(Text, default="")
    # Self-reported status; see display_status for what humans see.
    status: Mapped[str] = mapped_column(String(20), default="idle")
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    paused_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def is_offline(self, now: datetime, offline_after_seconds: float) -> bool:
        if self.last_heartbeat is None:
            return True
        return (now - self.last_heartbeat).total_seconds() > offline_after_seconds

    def display_status(self, now: datetime, offline_after_seconds: float) -> str:
        if self.is_offline(now, offline_after_seconds):
            return "offline"
        if self.paused:
            return "paused"
        return self.status

    def to_dict(self, now: datetime, offline_after_seconds: float) -> dict[str, Any]:
        return {
            "name": self.name,
            "owner_name": self.owner_name,
            "machine": self.machine,
            "skills": self.skills or [],
            "current_task_id": self.current_task_id,
            "approach": self.approach,
            "status": self.display_status(now, offline_after_seconds),
            "reported_status": self.status,
            "paused": self.paused,
            "paused_by": self.paused_by,
            "paused_reason": self.paused_reason,
            "last_heartbeat": iso(self.last_heartbeat),
            "registered_at": iso(self.registered_at),
        }


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    claimed_by: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    approach_note: Mapped[str] = mapped_column(Text, default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    output_links: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[str] = mapped_column(String(100))
    created_by_type: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "parent_id": self.parent_id,
            "status": self.status,
            "claimed_by": self.claimed_by,
            "approach_note": self.approach_note,
            "lease_expires_at": iso(self.lease_expires_at),
            "output_links": self.output_links or [],
            "created_by": self.created_by,
            "created_by_type": self.created_by_type,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    author_name: Mapped[str] = mapped_column(String(100))
    author_type: Mapped[str] = mapped_column(String(10))
    type: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    # Exactly one of channel / task_id is set.
    channel: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id"), nullable=True, index=True)
    reply_to: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    mentions: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Set for questions to humans; cleared (resolved) when a human answers.
    needs_human: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": iso(self.created_at),
            "author_name": self.author_name,
            "author_type": self.author_type,
            "type": self.type,
            "body": self.body,
            "channel": self.channel,
            "task_id": self.task_id,
            "reply_to": self.reply_to,
            "mentions": self.mentions or [],
            "needs_human": self.needs_human,
            "resolved_at": iso(self.resolved_at),
            "resolved_by": self.resolved_by,
        }


class MessageMention(Base):
    """One row per @mention, so inbox queries are a plain indexed lookup."""

    __tablename__ = "message_mentions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    # Lower-cased name, so mentions are case-insensitive.
    name: Mapped[str] = mapped_column(String(100), index=True)
