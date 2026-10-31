"""Claiming is atomic: with many concurrent claimers, exactly one wins."""

from __future__ import annotations

import threading

import pytest

from agentic_colab.workspace import WorkspaceError


def test_concurrent_claim_race_has_exactly_one_winner(ws):
    agents = [f"agent-{i}" for i in range(12)]
    for name in agents:
        ws.register_agent(name)

    for round_ in range(5):
        task = ws.create_task("alice", "human", f"contested task {round_}")
        barrier = threading.Barrier(len(agents))
        winners, losers, unexpected = [], [], []

        def attempt(name: str) -> None:
            barrier.wait()
            try:
                ws.claim_task(name, task["id"])
                winners.append(name)
            except WorkspaceError as e:
                (losers if e.code == "not_claimable" else unexpected).append((name, e.code))
            except Exception as e:  # pragma: no cover - would be a real bug
                unexpected.append((name, repr(e)))

        threads = [threading.Thread(target=attempt, args=(n,)) for n in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert unexpected == []
        assert len(winners) == 1, winners
        assert len(losers) == len(agents) - 1
        stored = ws.get_task(task["id"])
        assert stored["status"] == "claimed"
        assert stored["claimed_by"] == winners[0]
        # Exactly one claim recorded in the history log and in the thread.
        claims = [e for e in ws.list_events(limit=1000) if e["type"] == "task.claimed" and e["payload"]["task_id"] == task["id"]]
        assert len(claims) == 1


def test_finished_or_held_tasks_cannot_be_claimed(ws):
    ws.register_agent("a")
    ws.register_agent("b")
    task = ws.create_task("alice", "human", "t")
    ws.claim_task("a", task["id"])
    with pytest.raises(WorkspaceError) as err:
        ws.claim_task("b", task["id"])
    assert err.value.code == "not_claimable"
    assert err.value.details["claimed_by"] == "a"

    ws.update_task("a", "agent", task["id"], status="completed")
    with pytest.raises(WorkspaceError) as err:
        ws.claim_task("b", task["id"])
    assert err.value.details["task_status"] == "completed"


def test_only_holder_can_update(ws):
    ws.register_agent("a")
    ws.register_agent("b")
    task = ws.create_task("alice", "human", "t")
    ws.claim_task("a", task["id"])
    with pytest.raises(WorkspaceError) as err:
        ws.update_task("b", "agent", task["id"], status="completed")
    assert err.value.code == "not_your_task"
    # Humans can always change a task.
    assert ws.update_task("alice", "human", task["id"], status="failed")["status"] == "failed"
