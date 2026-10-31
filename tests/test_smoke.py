"""End to end: two EchoAgents and a simulated human share the workspace."""

from __future__ import annotations

import anyio
import httpx
import pytest

from runner import EchoAgent, Runner

from .conftest import wait_until


@pytest.mark.anyio
async def test_two_echo_agents_and_a_human(live_server):
    ws = live_server.ws
    agents = {name: EchoAgent(work_steps=2) for name in ("echo-a", "echo-b")}
    runners = [
        Runner(live_server.url, name, agent, owner_name="tester", step_interval=0.05, idle_interval=0.1,
               heartbeat_interval=0.3)
        for name, agent in agents.items()
    ]

    async with httpx.AsyncClient(base_url=live_server.url, headers={"X-Display-Name": "alice"}) as http:
        # The human sets up the charter and tasks through the dashboard API.
        r = await http.put("/api/charter", json={"objective": "Ship the demo", "base_version": 1})
        assert r.status_code == 200 and r.json()["version"] == 2
        stale = await http.put("/api/charter", json={"objective": "Old edit", "base_version": 1})
        assert stale.status_code == 409 and stale.json()["error"] == "charter_conflict"

        titles = ["Write docs", "Add tests", "Pick a license [ask]", "Fix bug", "Refactor", "Release"]
        task_ids = [(await http.post("/api/tasks", json={"title": t})).json()["id"] for t in titles]

        async with anyio.create_task_group() as tg:
            for r_ in runners:
                tg.start_soon(r_.run)
            await wait_until(lambda: all(r_.registered for r_ in runners), message="both runners registered")

            r = await http.post("/api/messages", json={"body": "Hi @echo-a, status?", "channel": "general"})
            assert r.status_code == 200 and r.json()["mentions"] == ["echo-a"]

            # An agent asks about the [ask] task; the human answers from the Needs-human panel.
            question = (await wait_until(lambda: ws.needs_human(), message="a question from an agent"))[0]
            assert "Pick a license" in question["body"]
            answer = await http.post("/api/messages", json={"body": "MIT, go ahead", "reply_to": question["id"]})
            assert answer.status_code == 200
            assert answer.json()["task_id"] == question["task_id"]  # lands in the task thread
            assert ws.needs_human() == []

            await wait_until(
                lambda: all(t["status"] == "completed" for t in ws.list_tasks()),
                timeout=30, message="all tasks completed",
            )
            for r_ in runners:
                r_.stop()

    tasks = ws.list_tasks()
    assert [t["id"] for t in tasks] == task_ids
    for t in tasks:
        assert t["output_links"] == [f"echo://tasks/{t['id']}/result"]
        assert t["claimed_by"] in agents
    # Each task was claimed exactly once (no double claims).
    claims = [e["payload"]["task_id"] for e in ws.list_events(limit=2000) if e["type"] == "task.claimed"]
    assert sorted(claims) == task_ids
    # Both agents did work and posted messages.
    assert sum(len(a.completed) for a in agents.values()) == len(titles)
    authors = {m["author_name"] for m in ws.list_messages(channel="general", limit=500)}
    assert {"echo-a", "echo-b", "alice"} <= authors
    # The mention was echoed back.
    general = ws.list_messages(channel="general", limit=500)
    assert any(m["author_name"] == "echo-a" and "echo: Hi @echo-a" in m["body"] for m in general)
    # The answer reached the asking agent, which acknowledged it in the thread.
    thread = ws.list_messages(task_id=question["task_id"], limit=500)
    assert any("MIT, go ahead" in m["body"] and m["author_type"] == "agent" for m in thread)
