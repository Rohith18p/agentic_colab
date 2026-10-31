"""ClaudeCodeAgent, driven with a fake `claude` command so no model is called."""

from __future__ import annotations

import json
import sys
import time

import anyio
import httpx
import pytest

from runner import ClaudeCodeAgent, Runner

from .conftest import wait_until

FAKE_CLAUDE = """\
import json, os, sys, time
args = sys.argv[1:]
out = os.path.join(os.getcwd(), "calls.jsonl")
with open(out, "a") as f:
    f.write(json.dumps({"args": args, "agent": os.environ.get("AGENT_NAME")}) + "\\n")
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
print(json.dumps({"type": "result", "total_cost_usd": 0.25, "result": "did one thing"}), flush=True)
"""


def make_agent(tmp_path, **kw) -> ClaudeCodeAgent:
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    playbook = tmp_path / "PLAYBOOK.md"
    playbook.write_text("Be a good swarm agent.")
    return ClaudeCodeAgent(
        command=[sys.executable, str(fake)], prompt_file=playbook, workdir=tmp_path / "work",
        pause_poll_seconds=0.2, **kw,
    )


def calls(tmp_path) -> list[dict]:
    path = tmp_path / "work" / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.anyio
async def test_shifts_pass_inbox_and_track_spend(live_server, tmp_path):
    agent = make_agent(tmp_path, max_shifts=2, total_budget_usd=10)
    runner = Runner(live_server.url, "cc-1", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with httpx.AsyncClient(base_url=live_server.url, headers={"X-Display-Name": "lead"}) as http:
        await http.post("/api/messages", json={"body": "@cc-1 please look at gravity", "channel": "general"})
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.run)
            await wait_until(lambda: agent.state.shifts >= 2, timeout=20, message="two shifts")
            runner.stop()

    made = calls(tmp_path)
    assert len(made) == 2 and made[0]["agent"] == "cc-1"
    args = made[0]["args"]
    prompt = args[args.index("-p") + 1]
    assert "please look at gravity" in prompt and 'agent_name="cc-1"' in prompt
    assert "please look at gravity" not in made[1]["args"][made[1]["args"].index("-p") + 1]
    assert args[args.index("--append-system-prompt") + 1] == "Be a good swarm agent."
    mcp = json.loads((tmp_path / "work" / ".mcp-workspace.json").read_text())
    assert mcp["mcpServers"]["board"]["url"].endswith("/mcp")

    state = json.loads((tmp_path / "work" / ".harness_state.json").read_text())
    assert state["shifts"] == 2 and state["spent_usd"] == pytest.approx(0.5)
    assert (tmp_path / "work" / "NOTEBOOK.md").exists()
    assert (tmp_path / "work" / "logs" / "shift-0001.jsonl").read_text().count("total_cost_usd") == 1


@pytest.mark.anyio
async def test_pause_stops_a_running_shift(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "30")
    agent = make_agent(tmp_path, max_shifts=1)
    runner = Runner(live_server.url, "cc-2", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(lambda: calls(tmp_path), timeout=10, message="shift started")
        started = time.monotonic()
        live_server.ws.pause_agent("cc-2", by="lead", reason="steering")
        await wait_until(lambda: agent.state.shifts >= 1, timeout=15, message="shift stopped")
        runner.stop()
    assert time.monotonic() - started < 12
    assert agent.state.last_stop == "paused"


@pytest.mark.anyio
async def test_budget_stops_new_shifts(live_server, tmp_path):
    agent = make_agent(tmp_path, total_budget_usd=0.25)
    runner = Runner(live_server.url, "cc-3", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(
            lambda: any("stopped starting new shifts" in m["body"] for m in live_server.ws.list_messages()),
            timeout=20, message="budget notice",
        )
        runner.stop()
    assert agent.state.shifts == 1 and len(calls(tmp_path)) == 1


def test_agent_env_changes_settings_between_shifts(tmp_path):
    agent = make_agent(tmp_path, total_budget_usd=1.0)
    (tmp_path / "work").mkdir()
    agent._reload_settings("x")  # no file: defaults stay
    assert agent.model == "claude-sonnet-5-5"
    (tmp_path / "work" / "agent.env").write_text(
        "# owner overrides\nAGENT_MODEL=claude-opus-5-5\nexport SHIFT_BUDGET_USD='2.5'\nTOTAL_BUDGET_USD=\nNOT_A_SETTING=1\n"
    )
    agent._announced_stop = True
    agent._reload_settings("x")
    assert agent.model == "claude-opus-5-5" and agent.shift_budget_usd == 2.5
    assert agent.total_budget_usd is None and agent._announced_stop is False


def test_self_update_reports_runner_changes(tmp_path):
    import subprocess

    from runner.claude_code import self_update

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    upstream, clone = tmp_path / "upstream", tmp_path / "clone"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    (upstream / "swarm").mkdir()
    (upstream / "swarm" / "AGENT_PROMPT.md").write_text("v1")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "v1")
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)

    assert self_update(clone) is False  # nothing new
    (upstream / "swarm" / "AGENT_PROMPT.md").write_text("v2")
    git(upstream, "commit", "-q", "-am", "playbook only")
    assert self_update(clone) is False and (clone / "swarm" / "AGENT_PROMPT.md").read_text() == "v2"
    (upstream / "runner").mkdir()
    (upstream / "runner" / "x.py").write_text("print(1)")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "runner change")
    assert self_update(clone) is True
    (clone / "runner" / "x.py").write_text("local edit")  # dirty tree: the pull must not clobber it
    (upstream / "runner" / "x.py").write_text("print(2)")
    git(upstream, "commit", "-q", "-am", "another runner change")
    assert self_update(clone) is False and (clone / "runner" / "x.py").read_text() == "local edit"
