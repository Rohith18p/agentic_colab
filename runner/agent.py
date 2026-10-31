"""The interface a real agent implements to run under `Runner`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .client import WorkspaceClient


class AgentContext:
    """What an agent sees during a step: its name and helpers for the workspace tools.

    Every helper is one MCP tool call. `call()` reaches any tool directly.
    """

    def __init__(self, client: WorkspaceClient, agent_name: str) -> None:
        self.client = client
        self.agent_name = agent_name
        self.inbox_cursor = 0

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        return await self.client.call(tool, **arguments)

    async def read_charter(self) -> dict[str, Any]:
        return await self.call("read_charter")

    async def read_inbox(self) -> list[dict[str, Any]]:
        """New messages for this agent since the last call."""
        result = await self.call("read_inbox", agent_name=self.agent_name, cursor=self.inbox_cursor)
        self.inbox_cursor = result["next_cursor"]
        return result["messages"]

    async def list_tasks(self, status: str | None = None, claimed_by: str | None = None) -> list[dict[str, Any]]:
        return (await self.call("list_tasks", status=status, claimed_by=claimed_by))["tasks"]

    async def list_messages(self, channel: str | None = None, task_id: int | None = None) -> list[dict[str, Any]]:
        return (await self.call("list_messages", channel=channel, task_id=task_id))["messages"]

    async def create_task(self, title: str, description: str = "", parent_id: int | None = None) -> dict[str, Any]:
        return await self.call(
            "create_task", agent_name=self.agent_name, title=title, description=description, parent_id=parent_id
        )

    async def claim_task(self, task_id: int, approach_note: str = "") -> dict[str, Any]:
        return await self.call("claim_task", agent_name=self.agent_name, task_id=task_id, approach_note=approach_note)

    async def update_task(self, task_id: int, **fields: Any) -> dict[str, Any]:
        """fields: status, approach_note, output_links, note."""
        return await self.call("update_task", agent_name=self.agent_name, task_id=task_id, **fields)

    async def release_task(self, task_id: int, reason: str = "") -> dict[str, Any]:
        return await self.call("release_task", agent_name=self.agent_name, task_id=task_id, reason=reason)

    async def post_message(self, body: str, type: str = "status", **target: Any) -> dict[str, Any]:
        """target: channel, task_id, reply_to, mentions."""
        return await self.call("post_message", agent_name=self.agent_name, body=body, type=type, **target)

    async def ask_human(self, question: str, task_id: int | None = None) -> dict[str, Any]:
        return await self.call("ask_human", agent_name=self.agent_name, question=question, task_id=task_id)


class Agent(ABC):
    """Plug a real agent in by subclassing this.

    The runner calls `step()` repeatedly, checking the pause state before each
    call and heartbeating in the background. Keep steps small (one decision or
    one tool action) so a pause takes effect quickly. Long-running work is fine
    too: heartbeats continue while a step runs.
    """

    skills: list[str] = []

    async def setup(self, ctx: AgentContext) -> None:  # noqa: B027 - optional hook
        """Called once after registering (and again after re-registering)."""

    @abstractmethod
    async def step(self, ctx: AgentContext) -> bool:
        """Do one unit of work. Return True if something was done, False if idle."""
