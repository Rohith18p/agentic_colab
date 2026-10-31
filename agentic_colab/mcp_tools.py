"""MCP tools for agents, served over Streamable HTTP at /mcp.

There are no tokens: each tool takes `agent_name`, the name the agent
registered with. Tools return JSON objects; expected failures come back as
tool errors (is_error=True) whose text is a JSON object with `error` and
`message` keys.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .workspace import Workspace, WorkspaceError

INSTRUCTIONS = """\
This is a shared workspace for AI agents and humans working on one project.
It stores and relays; it does not tell you what to do. Typical loop:
register_agent once, then repeatedly: get_control_state (stop acting while
paused), heartbeat (keeps your task leases alive), read_inbox, then do one
step of work (list_tasks / claim_task / update_task / post_message).
Read the charter for the project's objective and constraints. Channels:
#general (coordination), #decisions (proposals and decisions that spend shared
resources), #findings (results: post type "finding"; reply to a finding with
type "review" to replicate or challenge it), #compute (machines and shared
services such as model endpoints: what is up, where, and who runs it). Use ask_human when you need a
human decision. Never post secrets.
"""


def build_mcp_server(ws: Workspace) -> MCPServer:
    mcp = MCPServer(
        "agentic-colab",
        title="agentic_colab workspace",
        instructions=INSTRUCTIONS,
        version="0.1.0",
    )

    def call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except WorkspaceError as e:
            raise ToolError(json.dumps(e.to_dict())) from e

    @mcp.tool()
    def register_agent(
        agent_name: str, owner_name: str = "", machine: str = "", skills: list[str] | None = None
    ) -> dict[str, Any]:
        """Register (or re-register) this agent by name. Call once at startup.

        agent_name: unique name for this agent, e.g. "alice-claude-1" (letters, digits, _ . -).
        owner_name: the human who runs this agent. machine: where it runs. skills: free-form tags.
        Returns the agent record, the current control state (paused or not), and the
        lease/heartbeat timings to use. Re-registering keeps any pause in place.
        """
        return call(ws.register_agent, agent_name, owner_name=owner_name, machine=machine, skills=skills)

    @mcp.tool()
    def heartbeat(agent_name: str, status: str | None = None, approach: str | None = None) -> dict[str, Any]:
        """Report that this agent is alive. Send every few seconds (see heartbeat_interval_seconds).

        Extends the lease on every task this agent holds; without heartbeats a claimed
        task returns to open after the lease expires. Optionally report status
        (idle, working, blocked, waiting_on_human) and a one-line approach.
        Returns the control state, so a heartbeat also tells you if you are paused.
        """
        return call(ws.heartbeat, agent_name, status=status, approach=approach)

    @mcp.tool()
    def get_control_state(agent_name: str) -> dict[str, Any]:
        """Check whether a human has paused this agent. Call before every step.

        If `paused` is true, take no actions (only heartbeat) until it becomes false.
        """
        return call(ws.get_control_state, agent_name)

    @mcp.tool()
    def read_charter() -> dict[str, Any]:
        """Read the project charter: objective, constraints, libraries, compute available, notes."""
        return call(ws.read_charter)

    @mcp.tool()
    def read_inbox(agent_name: str, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        """Messages for this agent since `cursor`: @mentions of it, plus any message in
        threads of tasks it holds or created. Excludes its own messages.

        Pass the returned `next_cursor` as `cursor` next time to get only new messages.
        """
        return call(ws.read_inbox, agent_name, cursor=cursor, limit=limit)

    @mcp.tool()
    def list_messages(
        channel: str | None = None, task_id: int | None = None, after_id: int | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """List messages in a channel ("general", "decisions", "findings" or "compute") or in a task's thread (task_id), oldest first.

        With after_id, returns messages after that id; otherwise the latest `limit` messages.
        """
        return {"messages": call(ws.list_messages, channel=channel, task_id=task_id, after_id=after_id, limit=limit)}

    @mcp.tool()
    def post_message(
        agent_name: str,
        body: str,
        type: str = "status",
        channel: str | None = None,
        task_id: int | None = None,
        reply_to: int | None = None,
        mentions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Post a message as this agent.

        type: status, proposal, question, decision, human_input, finding or review.
        Use "finding" for a result (in #findings) and "review" with reply_to to
        replicate or challenge someone's finding.
        Target: a channel ("general", "decisions", "findings" or "compute") or a task thread (task_id); default "general".
        @name in the body mentions that agent or human; `mentions` adds more names.
        reply_to: id of the message you are answering.
        """
        return call(
            ws.post_message, agent_name, "agent", type, body, channel=channel, task_id=task_id,
            reply_to=reply_to, mentions=mentions,
        )

    @mcp.tool()
    def list_tasks(
        status: str | None = None, claimed_by: str | None = None, parent_id: int | None = None
    ) -> dict[str, Any]:
        """List tasks, optionally filtered by status (open, claimed, in_progress, needs_input,
        paused, completed, failed), by holder, or by parent task."""
        return {"tasks": call(ws.list_tasks, status=status, claimed_by=claimed_by, parent_id=parent_id)}

    @mcp.tool()
    def create_task(
        agent_name: str, title: str, description: str = "", parent_id: int | None = None
    ) -> dict[str, Any]:
        """Create a new open task (optionally a subtask of parent_id). Creating does not claim it."""
        return call(ws.create_task, agent_name, "agent", title, description=description, parent_id=parent_id)

    @mcp.tool()
    def claim_task(agent_name: str, task_id: int, approach_note: str = "") -> dict[str, Any]:
        """Claim an open task. Atomic: if several agents claim at once, exactly one wins and
        the others get a `not_claimable` error. The claim holds a lease that your heartbeats
        extend. approach_note: one or two lines on how you plan to do it.
        """
        return call(ws.claim_task, agent_name, task_id, approach_note=approach_note)

    @mcp.tool()
    def update_task(
        agent_name: str,
        task_id: int,
        status: str | None = None,
        approach_note: str | None = None,
        output_links: list[str] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Update a task you hold.

        status: in_progress, needs_input, paused, completed or failed. completed/failed
        end your claim. output_links are appended (URLs, file paths, commit hashes).
        note is posted to the task thread as a status message.
        """
        return call(
            ws.update_task, agent_name, "agent", task_id, status=status, approach_note=approach_note,
            output_links=output_links, note=note,
        )

    @mcp.tool()
    def release_task(agent_name: str, task_id: int, reason: str = "") -> dict[str, Any]:
        """Give back a task you hold so someone else can claim it. It returns to open."""
        return call(ws.release_task, agent_name, "agent", task_id, reason=reason)

    @mcp.tool()
    def ask_human(agent_name: str, question: str, task_id: int | None = None) -> dict[str, Any]:
        """Ask the humans a question. It appears in the dashboard's "Needs human" panel.

        If task_id is a task you hold, the question goes in its thread and the task is
        marked needs_input. The answer arrives in read_inbox as a reply to the question.
        """
        return call(ws.ask_human, agent_name, question, task_id=task_id)

    return mcp
