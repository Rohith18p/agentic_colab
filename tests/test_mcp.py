"""Round-trip over real Streamable HTTP with the official MCP client."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

EXPECTED_TOOLS = {
    "register_agent", "heartbeat", "read_charter", "read_inbox", "list_messages", "post_message",
    "list_tasks", "create_task", "claim_task", "update_task", "release_task", "ask_human",
    "get_control_state",
}


def error_of(result) -> dict:
    assert result.is_error
    text = result.content[0].text
    return json.loads(text[text.index("{"):])


@pytest.mark.anyio
async def test_mcp_round_trip(live_server):
    ws = live_server.ws
    url = f"{live_server.url}/mcp"
    async with Client(url) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == EXPECTED_TOOLS
        assert all(t.description for t in tools.tools)

        reg = await client.call_tool("register_agent", {"agent_name": "mcp-agent", "owner_name": "bob", "skills": ["py"]})
        assert not reg.is_error
        assert reg.structured_content["agent"]["name"] == "mcp-agent"
        assert reg.structured_content["control"]["paused"] is False

        charter = (await client.call_tool("read_charter", {})).structured_content
        assert "warning" in charter and charter["version"] == 1

        created = (await client.call_tool(
            "create_task", {"agent_name": "mcp-agent", "title": "via mcp", "description": "d"}
        )).structured_content
        tid = created["id"]
        assert created["status"] == "open"

        claimed = (await client.call_tool(
            "claim_task", {"agent_name": "mcp-agent", "task_id": tid, "approach_note": "just do it"}
        )).structured_content
        assert claimed["claimed_by"] == "mcp-agent" and claimed["status"] == "claimed"

        # A second claim is a structured error.
        await client.call_tool("register_agent", {"agent_name": "rival"})
        err = error_of(await client.call_tool("claim_task", {"agent_name": "rival", "task_id": tid}))
        assert err["error"] == "not_claimable" and err["claimed_by"] == "mcp-agent"

        # A human mentions the agent; it shows up in the inbox.
        ws.post_message("alice", "human", "human_input", "@mcp-agent please add tests", task_id=tid)
        inbox = (await client.call_tool("read_inbox", {"agent_name": "mcp-agent"})).structured_content
        assert [m["body"] for m in inbox["messages"]] == ["@mcp-agent please add tests"]
        again = (await client.call_tool(
            "read_inbox", {"agent_name": "mcp-agent", "cursor": inbox["next_cursor"]}
        )).structured_content
        assert again["messages"] == []

        q = (await client.call_tool(
            "ask_human", {"agent_name": "mcp-agent", "question": "Which license?", "task_id": tid}
        )).structured_content
        assert q["needs_human"] is True
        assert ws.get_task(tid)["status"] == "needs_input"
        assert [m["id"] for m in ws.needs_human()] == [q["id"]]

        hb = (await client.call_tool("heartbeat", {"agent_name": "mcp-agent", "status": "working"})).structured_content
        assert hb["leases_extended"] == [tid]

        done = (await client.call_tool("update_task", {
            "agent_name": "mcp-agent", "task_id": tid, "status": "completed",
            "output_links": ["https://example.com/pr/1"], "note": "shipped",
        })).structured_content
        assert done["status"] == "completed" and done["output_links"] == ["https://example.com/pr/1"]

        posted = (await client.call_tool(
            "post_message", {"agent_name": "mcp-agent", "body": "We use MIT.", "type": "decision", "channel": "decisions"}
        )).structured_content
        listed = (await client.call_tool("list_messages", {"channel": "decisions"})).structured_content
        assert [m["id"] for m in listed["messages"]] == [posted["id"]]

        tasks = (await client.call_tool("list_tasks", {"status": "completed"})).structured_content["tasks"]
        assert [t["id"] for t in tasks] == [tid]

        ws.pause_agent("mcp-agent", by="alice")
        control = (await client.call_tool("get_control_state", {"agent_name": "mcp-agent"})).structured_content
        assert control["paused"] is True
        err = error_of(await client.call_tool("post_message", {"agent_name": "mcp-agent", "body": "hi"}))
        assert err["error"] == "agent_paused"

        err = error_of(await client.call_tool("heartbeat", {"agent_name": "ghost"}))
        assert err["error"] == "not_registered"


@pytest.mark.anyio
async def test_concurrent_mcp_claims_have_one_winner(live_server):
    ws = live_server.ws
    names = [f"racer-{i}" for i in range(6)]
    for n in names:
        ws.register_agent(n)
    task = ws.create_task("alice", "human", "race me")
    url = f"{live_server.url}/mcp"
    results: dict[str, bool] = {}

    async def claim(name: str) -> None:
        async with Client(url) as client:
            r = await client.call_tool("claim_task", {"agent_name": name, "task_id": task["id"]})
            results[name] = not r.is_error

    async with anyio.create_task_group() as tg:
        for n in names:
            tg.start_soon(claim, n)
    winners = [n for n, ok in results.items() if ok]
    assert len(results) == len(names) and len(winners) == 1
    assert ws.get_task(task["id"])["claimed_by"] == winners[0]
