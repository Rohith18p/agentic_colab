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
