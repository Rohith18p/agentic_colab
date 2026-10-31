"""The #findings channel and the finding / review message types."""

from __future__ import annotations

import pytest

from agentic_colab.workspace import WorkspaceError


def test_finding_and_review_thread(ws):
    ws.register_agent("alpha")
    ws.register_agent("bravo")
    finding = ws.post_message(
        "alpha", "agent", "finding", "FINDING: free-fall g = 0.47 +/- 0.05 m/s^2 (n=12)", channel="findings"
    )
    assert finding["channel"] == "findings" and finding["type"] == "finding"

    review = ws.post_message(
        "bravo", "agent", "review", "@alpha replicated: g = 0.44 +/- 0.06", channel="findings",
        reply_to=finding["id"],
    )
    assert review["reply_to"] == finding["id"] and review["mentions"] == ["alpha"]
    assert [m["id"] for m in ws.read_inbox("alpha")["messages"]] == [review["id"]]
    assert [m["type"] for m in ws.list_messages(channel="findings")] == ["finding", "review"]
    assert "findings" in ws.snapshot()["settings"]["channels"]


def test_unknown_channel_is_rejected(ws):
    ws.register_agent("alpha")
    with pytest.raises(WorkspaceError) as e:
        ws.post_message("alpha", "agent", "finding", "x", channel="results")
    assert e.value.code == "invalid_channel"


@pytest.mark.anyio
async def test_boot_id_lets_dashboards_detect_restarts(live_server):
    import httpx

    async with httpx.AsyncClient(base_url=live_server.url) as http:
        boot = (await http.get("/api/state")).json()["boot_id"]
        assert boot and (await http.get("/api/state")).json()["boot_id"] == boot
        async with http.stream("GET", "/api/stream") as r:
            lines = r.aiter_lines()
            seen = [await anext(lines) for _ in range(5)]
    assert "event: hello" in seen and f'data: {{"boot_id": "{boot}"}}' in seen
    assert "compute" in live_server.ws.snapshot()["settings"]["channels"]
