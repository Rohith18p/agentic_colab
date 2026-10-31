"""Pausing stops an agent from acting; resuming lets it continue."""

from __future__ import annotations

import anyio
import httpx
import pytest

from agentic_colab.workspace import WorkspaceError
from runner import EchoAgent, Runner

from .conftest import wait_until


def test_paused_agent_cannot_act_but_keeps_its_lease(ws):
    ws.register_agent("a")
    task = ws.create_task("alice", "human", "t")
    other = ws.create_task("alice", "human", "other")
    ws.claim_task("a", task["id"])
    ws.update_task("a", "agent", task["id"], status="in_progress")

    ws.pause_agent("a", by="alice", reason="checking something")
    control = ws.get_control_state("a")
    assert control["paused"] is True and control["paused_by"] == "alice"

    for action in (
        lambda: ws.claim_task("a", other["id"]),
        lambda: ws.update_task("a", "agent", task["id"], status="completed"),
        lambda: ws.post_message("a", "agent", "status", "still here"),
        lambda: ws.create_task("a", "agent", "new"),
        lambda: ws.release_task("a", "agent", task["id"]),
        lambda: ws.ask_human("a", "may I?"),
    ):
        with pytest.raises(WorkspaceError) as err:
            action()
        assert err.value.code == "agent_paused"

    # Heartbeats still work and still extend the lease; the task stays held.
    hb = ws.heartbeat("a")
    assert hb["control"]["paused"] is True
    assert hb["leases_extended"] == [task["id"]]
    held = ws.get_task(task["id"])
    assert held["claimed_by"] == "a" and held["status"] == "paused"
    assert ws.get_task(other["id"])["status"] == "open"
    assert ws.list_agents()[0]["status"] == "paused"
    # A control message went to #general mentioning the agent.
    control_msgs = [m for m in ws.list_messages(channel="general") if m["type"] == "control"]
    assert control_msgs and control_msgs[-1]["mentions"] == ["a"]

    ws.resume_agent("a", by="alice")
    assert ws.get_control_state("a")["paused"] is False
    assert ws.get_task(task["id"])["status"] == "in_progress"
    assert ws.update_task("a", "agent", task["id"], status="completed")["status"] == "completed"


@pytest.mark.anyio
async def test_pause_stops_runner_and_resume_lets_it_continue(live_server):
    ws = live_server.ws
    headers = {"X-Display-Name": "alice"}
    runner = Runner(
        live_server.url, "echo-1", EchoAgent(work_steps=1),
        step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3,
    )
    async with httpx.AsyncClient(base_url=live_server.url, headers=headers) as http, anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(lambda: runner.registered, message="runner registered")

        r = await http.post("/api/agents/echo-1/pause", json={"reason": "hold on"})
        assert r.status_code == 200 and r.json()["paused"] is True
        await wait_until(lambda: runner.paused, message="runner notices pause")

        task = (await http.post("/api/tasks", json={"title": "while paused"})).json()
        steps_before = runner.steps
        await anyio.sleep(1.5)
        assert ws.get_task(task["id"])["status"] == "open", "paused agent must not claim"
        assert runner.steps == steps_before, "paused runner must not call step()"
        # Still heartbeating, so not offline.
        assert ws.list_agents()[0]["status"] == "paused"

        r = await http.post("/api/agents/echo-1/resume")
        assert r.status_code == 200 and r.json()["paused"] is False
        await wait_until(
            lambda: ws.get_task(task["id"])["status"] == "completed", message="task completed after resume"
        )
        assert ws.get_task(task["id"])["claimed_by"] == "echo-1"
        runner.stop()
