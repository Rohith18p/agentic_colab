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
    f.write(json.dumps({"args": args, "agent": os.environ.get("AGENT_NAME"),
                        "api_key": "ANTHROPIC_API_KEY" in os.environ}) + "\\n")
print(json.dumps({"type": "system", "subtype": "init"}), flush=True)
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
if os.environ.get("FAKE_LIMIT"):
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": "You've hit your session limit · resets 3pm", "total_cost_usd": 0}), flush=True)
    sys.exit(1)
print(json.dumps({"type": "result", "total_cost_usd": 0.25, "result": "did one thing"}), flush=True)
"""


def make_agent(tmp_path, **kw) -> ClaudeCodeAgent:
    fake = tmp_path / "fake_claude.py"
    fake.write_text(FAKE_CLAUDE)
    playbook = tmp_path / "PLAYBOOK.md"
    playbook.write_text("Be a good swarm agent.")
    kw.setdefault("claude_config", tmp_path / "no-claude-config.json")  # never the real ~/.claude.json
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


@pytest.mark.anyio
async def test_max_shifts_runs_that_many_then_exits(live_server, tmp_path):
    for run in (1, 2):  # counted per start: a second smoke test runs its own shift
        agent = make_agent(tmp_path, max_shifts=1)
        runner = Runner(live_server.url, "cc-max", agent, step_interval=0.05, idle_interval=0.1,
                        heartbeat_interval=0.3)
        with anyio.fail_after(20):
            await runner.run()  # returns on its own
        assert agent.done and agent.state.shifts == run and len(calls(tmp_path)) == run
    assert any("ran its 1 shift(s)" in m["body"] for m in live_server.ws.list_messages())


def test_shift_count_survives_a_self_update_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_SHIFTS_THIS_RUN", "1")  # what step() sets before os.execv
    assert make_agent(tmp_path, max_shifts=1)._shifts_this_run == 1
    assert make_agent(tmp_path, max_shifts=1)._shifts_this_run == 0  # read once, by the restarted runner


def test_agent_env_changes_settings_between_shifts(tmp_path):
    agent = make_agent(tmp_path, total_budget_usd=1.0)
    (tmp_path / "work").mkdir()
    agent._reload_settings("x")  # no file: defaults stay
    assert agent.model == "claude-opus-5-5"
    (tmp_path / "work" / "agent.env").write_text(
        "# owner overrides\nAGENT_MODEL=claude-sonnet-5-5\nexport SHIFT_TIMEOUT_SECONDS='60'\nNOT_A_SETTING=1\n"
    )
    agent._announced_stop = True
    agent._reload_settings("x")
    assert agent.model == "claude-sonnet-5-5" and agent.shift_timeout_seconds == 60
    assert agent._announced_stop is False


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


def test_self_update_notices_runner_changes_someone_else_pulled(tmp_path):
    import subprocess

    from runner.claude_code import git_head, self_update

    def git(cwd, *args):
        subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    upstream, clone = tmp_path / "upstream", tmp_path / "clone"
    (upstream / "runner").mkdir(parents=True)
    git(upstream, "init", "-q", "-b", "main")
    (upstream / "runner" / "x.py").write_text("print(1)")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "v1")
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True)
    loaded = git_head(clone)  # what a running agent's code came from

    (upstream / "runner" / "x.py").write_text("print(2)")
    git(upstream, "commit", "-q", "-am", "runner change")
    git(clone, "pull", "-q", "--ff-only")  # e.g. start.sh pulling for a second agent
    assert self_update(clone) is False  # the old check: nothing new to pull
    assert self_update(clone, loaded) is True  # but this process still runs the old runner
    assert self_update(clone, git_head(clone)) is False


@pytest.mark.anyio
async def test_swarm_end_stops_the_shift_and_the_runner(live_server, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("FAKE_SLEEP", "30")
    agent = make_agent(tmp_path)
    agent.end = datetime.now(timezone.utc) + timedelta(seconds=2)
    runner = Runner(live_server.url, "cc-end", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    started = time.monotonic()
    with anyio.fail_after(20):
        await runner.run()  # returns by itself once the agent is done
    assert time.monotonic() - started < 15
    assert agent.done and agent.state.last_stop == "swarm_end" and agent.state.shifts == 1


@pytest.mark.anyio
async def test_shifts_never_see_api_keys_and_report_to_activity(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-used")
    agent = make_agent(tmp_path, max_shifts=1)
    runner = Runner(live_server.url, "cc-plan", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(lambda: live_server.ws.list_messages(channel="activity"), timeout=20, message="shift report")
        runner.stop()
    assert calls(tmp_path)[0]["api_key"] is False
    report = live_server.ws.list_messages(channel="activity")[0]
    assert report["author_name"] == "cc-plan" and report["body"].startswith("Shift 1") and "did one thing" in report["body"]


@pytest.mark.anyio
async def test_usage_limit_pauses_new_shifts(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LIMIT", "1")
    agent = make_agent(tmp_path, limit_backoff_seconds=3600)
    runner = Runner(live_server.url, "cc-limit", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(lambda: agent.state.shifts >= 1, timeout=20, message="first shift")
        await anyio.sleep(1.0)  # several idle loops: none may start another shift
        runner.stop()
    assert agent.state.shifts == 1 and len(calls(tmp_path)) == 1
    assert agent.state.blocked_until is not None
    report = live_server.ws.list_messages(channel="activity")[0]["body"]
    assert "usage limit" in report and "session limit" in report


@pytest.mark.anyio
async def test_on_mention_agent_only_runs_when_asked(live_server, tmp_path):
    agent = make_agent(tmp_path, on_mention=True, shift_reports=False)
    runner = Runner(live_server.url, "explainer", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(lambda: runner.registered, message="registered")
        await anyio.sleep(0.8)
        assert calls(tmp_path) == []  # nobody asked: no shift, no usage
        live_server.ws.post_message("Nail", "human", "question", "@explainer what is bob-1 doing?", channel="ask")
        await wait_until(lambda: agent.state.shifts >= 1, timeout=20, message="shift after the question")
        runner.stop()
    prompt = calls(tmp_path)[0]["args"][calls(tmp_path)[0]["args"].index("-p") + 1]
    assert "what is bob-1 doing?" in prompt and "Answer the new messages" in prompt
    assert live_server.ws.list_messages(channel="activity") == []


@pytest.mark.anyio
async def test_shifts_get_the_research_mcp_servers_from_the_claude_config(live_server, tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"mcpServers": {
        "alphaxiv": {"type": "http", "url": "https://api.alphaxiv.org/mcp/v1"},
        "hf-mcp-server": {"type": "http", "url": "https://huggingface.co/mcp?login"},
        "playwright": {"type": "stdio", "command": "npx", "args": ["@playwright/mcp"]},
        "board": {"type": "http", "url": "http://elsewhere/mcp"},
    }}))
    agent = make_agent(tmp_path, max_shifts=1, claude_config=config,
                       extra_mcp_servers=["alphaxiv", "hf-mcp-server", "board", "not-installed"])
    runner = Runner(live_server.url, "cc-mcp", agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(lambda: agent.state.shifts >= 1, timeout=20, message="one shift")
        runner.stop()

    path = tmp_path / "work" / ".mcp-workspace.json"
    servers = json.loads(path.read_text())["mcpServers"]
    assert set(servers) == {"board", "alphaxiv", "hf-mcp-server"}  # only what was asked for and exists
    assert servers["board"]["url"].startswith(live_server.url)  # the swarm's board is never replaced
    assert servers["hf-mcp-server"]["url"] == "https://huggingface.co/mcp?login"
    assert path.stat().st_mode & 0o077 == 0
    args = calls(tmp_path)[0]["args"]
    allowed = args[args.index("--allowedTools") + 1].split()
    assert {"mcp__board", "mcp__alphaxiv", "mcp__hf-mcp-server", "Bash"} <= set(allowed)
    assert "mcp__alphaxiv__delete_folder" in args[args.index("--disallowedTools") + 1].split()


def test_extra_mcp_servers_default_and_hot_setting(tmp_path, monkeypatch):
    monkeypatch.delenv("EXTRA_MCP_SERVERS", raising=False)
    assert ClaudeCodeAgent().extra_mcp_servers == ["alphaxiv", "hf-mcp-server"]
    monkeypatch.setenv("EXTRA_MCP_SERVERS", "")
    assert ClaudeCodeAgent.from_env().extra_mcp_servers == []
    from runner.claude_code import HOT_SETTINGS
    attr, parse = HOT_SETTINGS["EXTRA_MCP_SERVERS"]
    assert attr == "extra_mcp_servers" and parse(" alphaxiv , wandb ") == ["alphaxiv", "wandb"]


def test_no_spend_caps_even_if_the_environment_sets_them(tmp_path, monkeypatch):
    import types
    monkeypatch.setenv("SHIFT_BUDGET_USD", "1.5")  # what old start_agent.sh exported
    monkeypatch.setenv("TOTAL_BUDGET_USD", "60")
    monkeypatch.setenv("AGENT_WORKDIR", str(tmp_path / "work"))
    agent = ClaudeCodeAgent.from_env()
    assert agent.shift_budget_usd is None and agent.total_budget_usd is None
    (tmp_path / "work").mkdir()
    (tmp_path / "PLAYBOOK.md").write_text("x")
    agent.prompt_file = tmp_path / "PLAYBOOK.md"
    ctx = types.SimpleNamespace(agent_name="x", client=types.SimpleNamespace(url="http://b/mcp"))
    assert "--max-budget-usd" not in agent._command(ctx, "p")


@pytest.mark.anyio
async def test_model_and_effort_tags_follow_the_settings(live_server, tmp_path):
    from runner.agent import AgentContext
    from runner.client import WorkspaceClient

    agent = make_agent(tmp_path, effort="high")
    async with WorkspaceClient(live_server.url) as client:
        await client.call("register_agent", agent_name="cc-tags", owner_name="t", machine="m", skills=["gpu-5090"])
        ctx = AgentContext(client, "cc-tags")
        await agent._sync_tags(ctx)
        tags = lambda: set(next(a for a in live_server.ws.list_agents() if a["name"] == "cc-tags")["skills"])
        assert tags() == {"gpu-5090", "model:opus-5-5", "effort:high"}
        agent.model, agent.effort = "claude-sonnet-5-5", "max"  # e.g. from agent.env
        await agent._sync_tags(ctx)
        assert tags() == {"gpu-5090", "model:sonnet-5-5", "effort:max"}
    (tmp_path / "work").mkdir(exist_ok=True)
    import types
    cmd = agent._command(types.SimpleNamespace(agent_name="cc-tags", client=types.SimpleNamespace(url="http://b/mcp")), "p")
    assert cmd[cmd.index("--effort") + 1] == "max"


def test_effort_falls_back_to_the_claude_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    agent = make_agent(tmp_path)
    assert agent.effective_effort() == "default"
    (tmp_path / "settings.json").write_text(json.dumps({"effortLevel": "xhigh"}))
    (tmp_path / "work").mkdir(exist_ok=True)
    assert agent.effective_effort() == "xhigh" and "--effort" not in agent._command(
        __import__("types").SimpleNamespace(agent_name="x", client=__import__("types").SimpleNamespace(url="u")), "p")
