"""Claims hold a lease; heartbeats extend it; expired tasks return to open."""

from __future__ import annotations

import time
from datetime import timedelta

from fastapi.testclient import TestClient

from agentic_colab.config import Settings
from agentic_colab.main import create_app
from agentic_colab.models import utcnow


def test_lease_expiry_returns_task_to_open(ws):
    t0 = utcnow()
    ws.register_agent("a", now=t0)
    task = ws.create_task("alice", "human", "t", now=t0)
    ws.claim_task("a", task["id"], now=t0)
    ws.update_task("a", "agent", task["id"], status="in_progress", now=t0)

    # Not yet expired.
    assert ws.reap_expired_leases(now=t0 + timedelta(seconds=29)) == []
    assert ws.get_task(task["id"])["status"] == "in_progress"

    # Expired.
    assert ws.reap_expired_leases(now=t0 + timedelta(seconds=31)) == [task["id"]]
    stored = ws.get_task(task["id"])
    assert stored["status"] == "open"
    assert stored["claimed_by"] is None
    assert stored["lease_expires_at"] is None
    assert ws.list_agents()[0]["current_task_id"] is None
    thread = ws.list_messages(task_id=task["id"])
    assert "Lease expired" in thread[-1]["body"]
    assert any(e["type"] == "task.lease_expired" for e in ws.list_events())


def test_heartbeat_extends_lease(ws):
    t0 = utcnow()
    ws.register_agent("a", now=t0)
    task = ws.create_task("alice", "human", "t", now=t0)
    ws.claim_task("a", task["id"], now=t0)

    hb = ws.heartbeat("a", now=t0 + timedelta(seconds=20))
    assert hb["leases_extended"] == [task["id"]]
    assert ws.reap_expired_leases(now=t0 + timedelta(seconds=40)) == []
    assert ws.get_task(task["id"])["claimed_by"] == "a"
    assert ws.reap_expired_leases(now=t0 + timedelta(seconds=51)) == [task["id"]]


def test_expired_lease_can_be_claimed_before_sweep(ws):
    t0 = utcnow()
    ws.register_agent("a", now=t0)
    ws.register_agent("b", now=t0)
    task = ws.create_task("alice", "human", "t", now=t0)
    ws.claim_task("a", task["id"], now=t0)
    claimed = ws.claim_task("b", task["id"], now=t0 + timedelta(seconds=31))
    assert claimed["claimed_by"] == "b"
    agents = {a["name"]: a for a in ws.list_agents()}
    assert agents["a"]["current_task_id"] is None
    assert agents["b"]["current_task_id"] == task["id"]


def test_background_sweeper_reopens_expired_tasks(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'sweep.db'}", lease_seconds=0.5, lease_sweep_seconds=0.1,
        mcp_allowed_hosts=[],
    )
    app = create_app(settings)
    ws = app.state.workspace
    with TestClient(app):  # runs the lifespan, which starts the sweeper
        ws.register_agent("a")
        task = ws.create_task("alice", "human", "t")
        ws.claim_task("a", task["id"])
        deadline = time.time() + 5
        while ws.get_task(task["id"])["status"] != "open":
            assert time.time() < deadline, "sweeper did not reopen the task"
            time.sleep(0.05)
