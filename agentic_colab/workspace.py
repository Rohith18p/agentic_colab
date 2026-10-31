"""All workspace state changes live here.

Both the MCP tools (agents) and the HTTP API (humans) call into `Workspace`.
Every method runs in its own transaction, writes an entry to the append-only
`events` log for anything it changes, and returns plain JSON-able dicts.

The workspace records and relays; it never assigns work. The only rules it
enforces are bookkeeping: claims are atomic, leases expire, paused agents
cannot act, and an agent may only update tasks it holds.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .db import init_db, make_engine, make_sessionmaker
from .models import (
    AGENT_REPORTABLE_STATUSES,
    CHANNELS,
    HELD_STATUSES,
    MESSAGE_TYPES,
    TASK_STATUSES,
    TERMINAL_STATUSES,
    Agent,
    CharterVersion,
    Event,
    Message,
    MessageMention,
    Task,
    iso,
    utcnow,
)
from .notifier import Notifier

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})")

CHARTER_WARNING = "Do not paste API keys or passwords here; anyone with the link can see this."


class WorkspaceError(Exception):
    """An expected failure, reported to agents and humans as a structured error."""

    def __init__(self, code: str, message: str, status: int = 400, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.details}


def parse_mentions(body: str) -> list[str]:
    return [m.rstrip(".-") for m in MENTION_RE.findall(body or "")]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


class Workspace:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.engine = make_engine(self.settings.database_url)
        self.Session = make_sessionmaker(self.engine)
        self.notifier = Notifier()
        init_db(self.engine)
        self._ensure_charter()

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _tx(self) -> Iterator[Session]:
        with self.Session() as s:
            with s.begin():
                yield s
            emitted = s.info.get("emitted", False)
        if emitted:
            self.notifier.notify()

    def _emit(self, s: Session, actor: str, actor_type: str, type_: str, payload: dict[str, Any]) -> None:
        s.add(Event(ts=utcnow(), actor_name=actor, actor_type=actor_type, type=type_, payload=payload))
        s.info["emitted"] = True

    def _lease_until(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.settings.lease_seconds)

    def _agent_dict(self, agent: Agent, now: datetime | None = None) -> dict[str, Any]:
        return agent.to_dict(now or utcnow(), self.settings.offline_after_seconds)

    def _control(self, agent: Agent) -> dict[str, Any]:
        return {
            "agent_name": agent.name,
            "paused": agent.paused,
            "paused_by": agent.paused_by,
            "paused_reason": agent.paused_reason,
            "instruction": (
                "You are paused by a human. Do not take any actions; keep sending heartbeats "
                "and check get_control_state again later."
                if agent.paused
                else "You are not paused. Carry on."
            ),
        }

    def _get_agent(self, s: Session, name: str) -> Agent:
        agent = s.scalar(select(Agent).where(Agent.name == name))
        if agent is None:
            raise WorkspaceError(
                "not_registered", f"No agent named {name!r}. Call register_agent first.", status=404
            )
        return agent

    def _active_agent(self, s: Session, name: str, now: datetime) -> Agent:
        """Look up an agent that is about to act. Paused agents, and every agent after SWARM_END, are refused."""
        agent = self._get_agent(s, name)
        agent.last_heartbeat = now
        if self.settings.swarm_end is not None and now >= self.settings.swarm_end:
            raise WorkspaceError(
                "swarm_ended",
                f"The swarm ended at {iso(self.settings.swarm_end)}; agents can no longer change anything. Stop now.",
                status=423,
            )
        if agent.paused:
            raise WorkspaceError(
                "agent_paused",
                f"Agent {name!r} is paused by {agent.paused_by or 'a human'}; it may not act until resumed.",
                status=423,
                paused_by=agent.paused_by,
                paused_reason=agent.paused_reason,
            )
        return agent

    def _get_task(self, s: Session, task_id: int) -> Task:
        task = s.get(Task, task_id, populate_existing=True)
        if task is None:
            raise WorkspaceError("task_not_found", f"No task with id {task_id}.", status=404)
        return task

    def _post(
        self,
        s: Session,
        *,
        author: str,
        author_type: str,
        type_: str,
        body: str,
        channel: str | None = None,
        task_id: int | None = None,
        reply_to: int | None = None,
        mentions: list[str] | None = None,
        needs_human: bool = False,
        now: datetime | None = None,
    ) -> Message:
        if type_ not in MESSAGE_TYPES:
            raise WorkspaceError("invalid_message_type", f"Message type must be one of {list(MESSAGE_TYPES)}.")
        body = (body or "").strip()
        if not body:
            raise WorkspaceError("empty_body", "Message body must not be empty.")
        if channel and task_id:
            raise WorkspaceError("invalid_target", "Give either a channel or a task_id, not both.")
        if task_id is not None:
            self._get_task(s, task_id)
        else:
            channel = channel or "general"
            if channel not in CHANNELS:
                raise WorkspaceError("invalid_channel", f"Channel must be one of {list(CHANNELS)} (or use task_id).")
        if reply_to is not None and s.get(Message, reply_to) is None:
            raise WorkspaceError("message_not_found", f"No message with id {reply_to}.", status=404)

        names = _dedupe(list(mentions or []) + parse_mentions(body))
        msg = Message(
            created_at=now or utcnow(),
            author_name=author,
            author_type=author_type,
            type=type_,
            body=body,
            channel=channel,
            task_id=task_id,
            reply_to=reply_to,
            mentions=names,
            needs_human=needs_human,
        )
        s.add(msg)
        s.flush()
        for name in names:
            s.add(MessageMention(message_id=msg.id, name=name.lower()))
        self._emit(s, author, author_type, "message.posted", msg.to_dict())
        return msg

    def _ensure_charter(self) -> None:
        with self._tx() as s:
            if s.scalar(select(CharterVersion.id).limit(1)) is None:
                s.add(CharterVersion(version=1, edited_by="system", created_at=utcnow()))

    # ------------------------------------------------------------------ charter

    def read_charter(self) -> dict[str, Any]:
        with self._tx() as s:
            current = s.scalar(select(CharterVersion).order_by(CharterVersion.version.desc()).limit(1))
            return {**current.to_dict(), "warning": CHARTER_WARNING}

    def update_charter(self, editor: str, fields: dict[str, str], base_version: int | None = None) -> dict[str, Any]:
        with self._tx() as s:
            current = s.scalar(select(CharterVersion).order_by(CharterVersion.version.desc()).limit(1))
            if base_version is not None and base_version != current.version:
                raise WorkspaceError(
                    "charter_conflict",
                    f"The charter was changed by {current.edited_by} (now version {current.version}) "
                    f"since you started editing version {base_version}. Reload and re-apply your edit.",
                    status=409,
                    current_version=current.version,
                )
            values = {f: str(fields.get(f, getattr(current, f)) or "") for f in CharterVersion.FIELDS}
            new = CharterVersion(version=current.version + 1, edited_by=editor, created_at=utcnow(), **values)
            s.add(new)
            s.flush()
            changed = [f for f in CharterVersion.FIELDS if values[f] != getattr(current, f)]
            self._emit(s, editor, "human", "charter.updated", {"version": new.version, "changed": changed})
            return {**new.to_dict(), "warning": CHARTER_WARNING}

    def charter_history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._tx() as s:
            rows = s.scalars(select(CharterVersion).order_by(CharterVersion.version.desc()).limit(limit))
            return [r.to_dict() for r in rows]

    # ------------------------------------------------------------------ agents

    def register_agent(
        self,
        name: str,
        owner_name: str = "",
        machine: str = "",
        skills: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not NAME_RE.match(name or ""):
            raise WorkspaceError(
                "invalid_name", "Agent names are 1-64 characters: letters, digits, '_', '.', '-'."
            )
        now = now or utcnow()
        with self._tx() as s:
            agent = s.scalar(select(Agent).where(Agent.name == name))
            new = agent is None
            if new:
                agent = Agent(name=name, registered_at=now, status="idle", skills=[])
                s.add(agent)
            agent.owner_name = owner_name or agent.owner_name or ""
            agent.machine = machine or agent.machine or ""
            if skills is not None:
                # Merge, don't replace: a runner restart re-registers with its launch-time tags,
                # and must not wipe tags the agent added itself (update_skills removes tags).
                agent.skills = _dedupe(list(agent.skills or []) + list(skills))
            agent.last_heartbeat = now
            s.flush()
            self._emit(
                s,
                name,
                "agent",
                "agent.registered" if new else "agent.reregistered",
                {"name": name, "owner_name": agent.owner_name, "machine": agent.machine, "skills": agent.skills},
            )
            return {
                "agent": self._agent_dict(agent, now),
                "control": self._control(agent),
                "lease_seconds": self.settings.lease_seconds,
                "heartbeat_interval_seconds": max(1.0, min(15.0, self.settings.lease_seconds / 4)),
                "offline_after_seconds": self.settings.offline_after_seconds,
            }

    def heartbeat(
        self,
        name: str,
        status: str | None = None,
        approach: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or utcnow()
        if status is not None and status not in AGENT_REPORTABLE_STATUSES:
            raise WorkspaceError("invalid_status", f"status must be one of {list(AGENT_REPORTABLE_STATUSES)}.")
        with self._tx() as s:
            agent = self._get_agent(s, name)
            was_offline = agent.is_offline(now, self.settings.offline_after_seconds)
            changes: dict[str, Any] = {}
            if status is not None and status != agent.status:
                changes["status"] = status
                agent.status = status
            if approach is not None and approach != agent.approach:
                changes["approach"] = approach
                agent.approach = approach
            agent.last_heartbeat = now
            res = s.execute(
                update(Task)
                .where(Task.claimed_by == name, Task.status.in_(HELD_STATUSES))
                .values(lease_expires_at=self._lease_until(now))
                .returning(Task.id)
            )
            extended = [row[0] for row in res]
            if changes or was_offline:
                self._emit(s, name, "agent", "agent.status", {"name": name, "online": True, **changes})
            return {
                "agent": self._agent_dict(agent, now),
                "control": self._control(agent),
                "leases_extended": extended,
                "lease_expires_at": iso(self._lease_until(now)) if extended else None,
            }

    def update_skills(
        self,
        name: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """An agent adds or removes its own skill tags (what its machine can do)."""
        now = now or utcnow()
        with self._tx() as s:
            agent = self._active_agent(s, name, now)
            drop = {t.strip().lower() for t in (remove or []) if t.strip()}
            kept = [t for t in (agent.skills or []) if t.lower() not in drop]
            new = _dedupe(kept + [t.strip()[:64] for t in (add or []) if t.strip()])
            if new != (agent.skills or []):
                agent.skills = new
                self._emit(s, name, "agent", "agent.skills", {"name": name, "skills": new})
            return self._agent_dict(agent, now)

    def get_control_state(self, name: str) -> dict[str, Any]:
        with self._tx() as s:
            return self._control(self._get_agent(s, name))

    def list_agents(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or utcnow()
        with self._tx() as s:
            return [self._agent_dict(a, now) for a in s.scalars(select(Agent).order_by(Agent.name))]

    def pause_agent(self, name: str, by: str, reason: str = "") -> dict[str, Any]:
        with self._tx() as s:
            agent = self._get_agent(s, name)
            if not agent.paused:
                agent.paused = True
                agent.paused_by = by
                agent.paused_reason = reason or None
                # The task keeps its claim and lease; it is only marked paused.
                if agent.current_task_id:
                    task = s.get(Task, agent.current_task_id)
                    if task and task.claimed_by == name and task.status in ("claimed", "in_progress"):
                        task.status = "paused"
                        task.updated_at = utcnow()
                body = f"@{name} paused by {by}." + (f" Reason: {reason}" if reason else "")
                self._post(s, author=by, author_type="human", type_="control", body=body, channel="general")
                self._emit(s, by, "human", "agent.paused", {"name": name, "reason": reason})
            return self._agent_dict(agent)

    def resume_agent(self, name: str, by: str) -> dict[str, Any]:
        with self._tx() as s:
            agent = self._get_agent(s, name)
            if agent.paused:
                agent.paused = False
                agent.paused_by = None
                agent.paused_reason = None
                if agent.current_task_id:
                    task = s.get(Task, agent.current_task_id)
                    if task and task.claimed_by == name and task.status == "paused":
                        task.status = "in_progress"
                        task.updated_at = utcnow()
                self._post(
                    s, author=by, author_type="human", type_="control", body=f"@{name} resumed by {by}.",
                    channel="general",
                )
                self._emit(s, by, "human", "agent.resumed", {"name": name})
            return self._agent_dict(agent)

    # ------------------------------------------------------------------ messages

    def post_message(
        self,
        author: str,
        author_type: str,
        type_: str,
        body: str,
        channel: str | None = None,
        task_id: int | None = None,
        reply_to: int | None = None,
        mentions: list[str] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or utcnow()
        with self._tx() as s:
            extra_mentions: list[str] = []
            if author_type == "agent":
                self._active_agent(s, author, now)
                if type_ == "control":
                    raise WorkspaceError("forbidden_type", "Only humans post control messages.", status=403)
            elif reply_to is not None:
                # A human replying to an open question answers it.
                question = s.get(Message, reply_to)
                if question is not None and question.needs_human and question.resolved_at is None:
                    question.resolved_at = now
                    question.resolved_by = author
                    extra_mentions.append(question.author_name)
                    if task_id is None and channel is None:
                        task_id, channel = question.task_id, question.channel
                    self._emit(s, author, author_type, "question.resolved", {"message_id": question.id})
            msg = self._post(
                s, author=author, author_type=author_type, type_=type_, body=body, channel=channel,
                task_id=task_id, reply_to=reply_to, mentions=(mentions or []) + extra_mentions, now=now,
            )
            return msg.to_dict()

    def ask_human(self, name: str, question: str, task_id: int | None = None, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        with self._tx() as s:
            agent = self._active_agent(s, name, now)
            msg = self._post(
                s, author=name, author_type="agent", type_="question", body=question,
                task_id=task_id, channel=None if task_id else "general", needs_human=True, now=now,
            )
            agent.status = "waiting_on_human"
            if task_id is not None:
                task = self._get_task(s, task_id)
                if task.claimed_by == name and task.status in HELD_STATUSES:
                    task.status = "needs_input"
                    task.updated_at = now
                    task.lease_expires_at = self._lease_until(now)
            self._emit(s, name, "agent", "agent.status", {"name": name, "status": "waiting_on_human"})
            return msg.to_dict()

    def resolve_question(self, message_id: int, by: str) -> dict[str, Any]:
        with self._tx() as s:
            msg = s.get(Message, message_id)
            if msg is None or not msg.needs_human:
                raise WorkspaceError("question_not_found", f"No question with id {message_id}.", status=404)
            if msg.resolved_at is None:
                msg.resolved_at = utcnow()
                msg.resolved_by = by
                self._emit(s, by, "human", "question.resolved", {"message_id": message_id})
            return msg.to_dict()

    def list_messages(
        self,
        channel: str | None = None,
        task_id: int | None = None,
        after_id: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Messages in one channel or task thread, oldest first.

        With after_id, returns up to `limit` messages after it; otherwise the latest `limit`.
        """
        limit = max(1, min(limit, 500))
        with self._tx() as s:
            q = select(Message)
            if task_id is not None:
                q = q.where(Message.task_id == task_id)
            else:
                q = q.where(Message.channel == (channel or "general"))
            if after_id is not None:
                rows = list(s.scalars(q.where(Message.id > after_id).order_by(Message.id).limit(limit)))
            else:
                rows = list(s.scalars(q.order_by(Message.id.desc()).limit(limit)))[::-1]
            return [m.to_dict() for m in rows]

    def read_inbox(self, name: str, cursor: int = 0, limit: int = 100, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        limit = max(1, min(limit, 500))
        with self._tx() as s:
            agent = self._get_agent(s, name)
            agent.last_heartbeat = now
            mentioned = select(MessageMention.message_id).where(MessageMention.name == name.lower())
            my_tasks = select(Task.id).where(or_(Task.claimed_by == name, Task.created_by == name))
            rows = s.scalars(
                select(Message)
                .where(
                    Message.id > cursor,
                    Message.author_name != name,
                    or_(Message.id.in_(mentioned), Message.task_id.in_(my_tasks)),
                )
                .order_by(Message.id)
                .limit(limit)
            ).all()
            next_cursor = rows[-1].id if rows else cursor
            return {"messages": [m.to_dict() for m in rows], "next_cursor": next_cursor}

    def needs_human(self) -> list[dict[str, Any]]:
        with self._tx() as s:
            rows = s.scalars(
                select(Message).where(Message.needs_human.is_(True), Message.resolved_at.is_(None)).order_by(Message.id)
            )
            return [m.to_dict() for m in rows]

    # ------------------------------------------------------------------ tasks

    def create_task(
        self,
        actor: str,
        actor_type: str,
        title: str,
        description: str = "",
        parent_id: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or utcnow()
        title = (title or "").strip()
        if not title:
            raise WorkspaceError("empty_title", "Task title must not be empty.")
        with self._tx() as s:
            if actor_type == "agent":
                self._active_agent(s, actor, now)
            if parent_id is not None:
                self._get_task(s, parent_id)
            task = Task(
                title=title, description=description or "", parent_id=parent_id, status="open",
                created_by=actor, created_by_type=actor_type, created_at=now, updated_at=now, output_links=[],
            )
            s.add(task)
            s.flush()
            self._emit(s, actor, actor_type, "task.created", task.to_dict())
            return task.to_dict()

    def list_tasks(
        self, status: str | None = None, claimed_by: str | None = None, parent_id: int | None = None
    ) -> list[dict[str, Any]]:
        with self._tx() as s:
            q = select(Task).order_by(Task.id)
            if status is not None:
                q = q.where(Task.status == status)
            if claimed_by is not None:
                q = q.where(Task.claimed_by == claimed_by)
            if parent_id is not None:
                q = q.where(Task.parent_id == parent_id)
            return [t.to_dict() for t in s.scalars(q)]

    def get_task(self, task_id: int) -> dict[str, Any]:
        with self._tx() as s:
            task = self._get_task(s, task_id)
            subtasks = s.scalars(select(Task).where(Task.parent_id == task_id).order_by(Task.id))
            return {**task.to_dict(), "subtasks": [t.to_dict() for t in subtasks]}

    def claim_task(self, name: str, task_id: int, approach_note: str = "", now: datetime | None = None) -> dict[str, Any]:
        """Atomically claim a task. Only one concurrent caller can win.

        A task is claimable when it is open, or when its holder's lease has
        already expired but the background sweep has not reopened it yet.
        """
        now = now or utcnow()
        with self._tx() as s:
            agent = self._active_agent(s, name, now)
            previous = s.scalar(select(Task.claimed_by).where(Task.id == task_id))
            result = s.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    or_(
                        Task.status == "open",
                        and_(Task.status.in_(HELD_STATUSES), Task.lease_expires_at < now),
                    ),
                )
                .values(
                    status="claimed",
                    claimed_by=name,
                    approach_note=approach_note or "",
                    lease_expires_at=self._lease_until(now),
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                task = self._get_task(s, task_id)
                raise WorkspaceError(
                    "not_claimable",
                    f"Task {task_id} is {task.status}"
                    + (f" (held by {task.claimed_by})" if task.claimed_by else "")
                    + "; it cannot be claimed.",
                    status=409,
                    task_status=task.status,
                    claimed_by=task.claimed_by,
                )
            if previous and previous != name:
                s.execute(
                    update(Agent)
                    .where(Agent.name == previous, Agent.current_task_id == task_id)
                    .values(current_task_id=None)
                    .execution_options(synchronize_session=False)
                )
            task = self._get_task(s, task_id)
            agent.current_task_id = task_id
            agent.status = "working"
            if approach_note:
                agent.approach = approach_note
            body = f"{name} claimed this task." + (f" Approach: {approach_note}" if approach_note else "")
            self._post(s, author=name, author_type="agent", type_="status", body=body, task_id=task_id, now=now)
            self._emit(s, name, "agent", "task.claimed", {"task_id": task_id, "approach_note": approach_note})
            return task.to_dict()

    def update_task(
        self,
        actor: str,
        actor_type: str,
        task_id: int,
        status: str | None = None,
        approach_note: str | None = None,
        output_links: list[str] | None = None,
        note: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = now or utcnow()
        if status is not None and status not in TASK_STATUSES:
            raise WorkspaceError("invalid_status", f"status must be one of {list(TASK_STATUSES)}.")
        with self._tx() as s:
            agent = self._active_agent(s, actor, now) if actor_type == "agent" else None
            task = self._get_task(s, task_id)
            if agent is not None:
                if task.claimed_by != actor or task.status not in HELD_STATUSES:
                    raise WorkspaceError(
                        "not_your_task", f"Task {task_id} is not held by {actor}; claim it first.", status=403
                    )
                if status in ("open", "claimed"):
                    raise WorkspaceError(
                        "invalid_status", "Use release_task to give a task back, or claim_task to claim one."
                    )
            changes: dict[str, Any] = {}
            if status is not None and status != task.status:
                changes["status"] = {"from": task.status, "to": status}
                task.status = status
            if approach_note is not None and approach_note != task.approach_note:
                changes["approach_note"] = approach_note
                task.approach_note = approach_note
                if agent is not None:
                    agent.approach = approach_note
            if output_links:
                merged = _dedupe(list(task.output_links or []) + list(output_links))
                if merged != task.output_links:
                    changes["output_links"] = merged
                    task.output_links = merged
            if task.status in TERMINAL_STATUSES or task.status == "open":
                task.lease_expires_at = None
                if task.status == "open":
                    task.claimed_by = None
                self._clear_current_task(s, task_id)
            elif task.claimed_by:
                task.lease_expires_at = self._lease_until(now)
                if agent is not None:
                    agent.status = "waiting_on_human" if task.status == "needs_input" else "working"
            task.updated_at = now
            if note or "status" in changes:
                body = note or f"Status changed to {task.status}."
                if note and "status" in changes:
                    body = f"[{task.status}] {note}"
                self._post(s, author=actor, author_type=actor_type, type_="status", body=body, task_id=task_id, now=now)
            if changes:
                self._emit(s, actor, actor_type, "task.updated", {"task_id": task_id, **changes})
            return task.to_dict()

    def release_task(self, actor: str, actor_type: str, task_id: int, reason: str = "", now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        with self._tx() as s:
            if actor_type == "agent":
                self._active_agent(s, actor, now)
            task = self._get_task(s, task_id)
            if actor_type == "agent" and task.claimed_by != actor:
                raise WorkspaceError("not_your_task", f"Task {task_id} is not held by {actor}.", status=403)
            if task.status in TERMINAL_STATUSES:
                raise WorkspaceError("task_finished", f"Task {task_id} is already {task.status}.", status=409)
            previous = task.claimed_by
            task.status = "open"
            task.claimed_by = None
            task.lease_expires_at = None
            task.updated_at = now
            self._clear_current_task(s, task_id)
            body = f"{actor} released this task" + (f" (was held by {previous})" if previous and previous != actor else "")
            body += f": {reason}" if reason else "."
            self._post(s, author=actor, author_type=actor_type, type_="status", body=body, task_id=task_id, now=now)
            self._emit(s, actor, actor_type, "task.released", {"task_id": task_id, "previous": previous, "reason": reason})
            return task.to_dict()

    def _clear_current_task(self, s: Session, task_id: int) -> None:
        for agent in s.scalars(select(Agent).where(Agent.current_task_id == task_id)):
            agent.current_task_id = None
            agent.approach = ""
            if agent.status in ("working", "waiting_on_human", "blocked"):
                agent.status = "idle"

    def reap_expired_leases(self, now: datetime | None = None) -> list[int]:
        """Return tasks whose lease has expired to `open`. Run periodically."""
        now = now or utcnow()
        reopened = []
        with self._tx() as s:
            expired = s.scalars(
                select(Task).where(Task.status.in_(HELD_STATUSES), Task.lease_expires_at < now)
            ).all()
            for task in expired:
                holder = task.claimed_by
                # Conditional, so a heartbeat that lands meanwhile wins.
                res = s.execute(
                    update(Task)
                    .where(Task.id == task.id, Task.status.in_(HELD_STATUSES), Task.lease_expires_at < now)
                    .values(status="open", claimed_by=None, lease_expires_at=None, updated_at=now)
                    .execution_options(synchronize_session=False)
                )
                if res.rowcount != 1:
                    continue
                reopened.append(task.id)
                self._clear_current_task(s, task.id)
                self._post(
                    s, author="workspace", author_type="system", type_="status", task_id=task.id, now=now,
                    body=f"Lease expired (held by {holder}, no heartbeat); task returned to open.",
                )
                self._emit(s, "workspace", "system", "task.lease_expired", {"task_id": task.id, "previous": holder})
        return reopened

    # ------------------------------------------------------------------ overview

    def list_events(self, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._tx() as s:
            rows = s.scalars(select(Event).where(Event.id > after_id).order_by(Event.id).limit(limit))
            return [e.to_dict() for e in rows]

    def last_event_id(self) -> int:
        with self._tx() as s:
            return s.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0

    def snapshot(self) -> dict[str, Any]:
        """Everything the dashboard shows except message lists."""
        return {
            "server_time": iso(utcnow()),
            "charter": self.read_charter(),
            "agents": self.list_agents(),
            "tasks": self.list_tasks(),
            "needs_human": self.needs_human(),
            "last_event_id": self.last_event_id(),
            "settings": {
                "lease_seconds": self.settings.lease_seconds,
                "offline_after_seconds": self.settings.offline_after_seconds,
                "channels": list(CHANNELS),
                "swarm_end": iso(self.settings.swarm_end),
            },
        }
