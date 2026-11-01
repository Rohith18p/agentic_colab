"""The dashboard's read endpoints: the page itself, paging back through a channel, one message, compute."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.anyio
async def test_dashboard_reads(live_server):
    ws = live_server.ws
    ids = [ws.post_message("alice", "human", "human_input", f"m{i}", channel="general")["id"] for i in range(5)]
    async with httpx.AsyncClient(base_url=live_server.url) as http:
        page = (await http.get("/")).text
        assert "/static/app.js?v=" in page and "{{" not in page

        older = (await http.get("/api/messages", params={"channel": "general", "before_id": ids[3], "limit": 2})).json()
        assert [m["id"] for m in older] == ids[1:3]

        one = await http.get(f"/api/messages/{ids[4]}")
        assert one.status_code == 200 and one.json()["body"] == "m4"
        assert (await http.get("/api/messages/999999")).status_code == 404

        # An unreachable queue: the endpoint says so instead of failing.
        ws.settings.cosmos_queue_url = "http://127.0.0.1:9"
        compute = (await http.get("/api/compute")).json()
        assert compute["ok"] is False and compute["url"]


@pytest.mark.anyio
async def test_humans_can_move_or_remove_the_swarm_end(live_server, settings):
    from agentic_colab.workspace import Workspace

    async with httpx.AsyncClient(base_url=live_server.url, headers={"X-Display-Name": "alice"}) as http:
        r = await http.put("/api/settings/swarm_end", json={"swarm_end": "2030-01-01T08:00:00-05:00"})
        assert r.status_code == 200 and r.json()["swarm_end"] == "2030-01-01T13:00:00Z"
        assert (await http.get("/api/settings")).json()["swarm_end"] == "2030-01-01T13:00:00Z"
        assert (await http.put("/api/settings/swarm_end", json={"swarm_end": "tomorrow"})).status_code == 400
        r = await http.put("/api/settings/swarm_end", json={"swarm_end": None})
        assert r.json()["swarm_end"] is None
    # Kept on disk: a restarted board (with SWARM_END still in its environment) keeps the human's choice.
    settings.swarm_end = live_server.ws.settings.swarm_end or __import__("datetime").datetime(2026, 1, 1)
    assert Workspace(settings).settings.swarm_end is None
    assert any("removed the swarm end" in m["body"] for m in live_server.ws.list_messages(channel="system"))
