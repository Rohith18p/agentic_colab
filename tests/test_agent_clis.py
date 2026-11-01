"""Codex and Antigravity (agy) shifts, driven with fake CLIs so no model is called."""

from __future__ import annotations

import json
import sys
import types

import anyio
import pytest

from runner import Runner
from runner.__main__ import load_agent
from runner.agy import AgyAgent
from runner.codex import CodexAgent

from .conftest import wait_until

# Both fakes log their arguments and whether an API key reached them, then print events in the
# real CLI's format (checked against `codex exec --json` and `agy -p --output-format stream-json`).
FAKE_CODEX = """\
import json, os, sys
with open(os.path.join(os.getcwd(), "calls.jsonl"), "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "api_key": "OPENAI_API_KEY" in os.environ}) + "\\n")
print(json.dumps({"type": "thread.started", "thread_id": "t"}), flush=True)
if os.environ.get("FAKE_LIMIT"):
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. Try again later."}), flush=True)
    sys.exit(1)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "did a codex thing"}}), flush=True)
print(json.dumps({"type": "turn.completed", "usage": {}}), flush=True)
"""

FAKE_AGY = """\
import json, os, sys
with open(os.path.join(os.getcwd(), "calls.jsonl"), "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "api_key": "OPENAI_API_KEY" in os.environ}) + "\\n")
step = lambda i, **kw: print(json.dumps({"event": "step_update", "step_update": {
    "step_index": i, "state": "DONE", "step_type": "agent_response", **kw}}), flush=True)
print(json.dumps({"event": "init", "init": {"model": "m"}}), flush=True)
step(1, text_delta="looking around")
if os.environ.get("FAKE_LIMIT"):
    print(json.dumps({"event": "result", "result": {"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota exceeded"}}))
    sys.exit(1)
step(2, text_delta="did an agy thing")
print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "did an agy thing"}}), flush=True)
"""


def make(cls, tmp_path, monkeypatch, fake: str, env_cmd: str):
    script = tmp_path / "fake_cli.py"
    script.write_text(fake)
    playbook = tmp_path / "PLAYBOOK.md"
    playbook.write_text("Be a good swarm agent.")
    monkeypatch.setenv(env_cmd, f"{sys.executable} {script}")
    monkeypatch.setenv("AGENT_PROMPT_FILE", str(playbook))
    monkeypatch.setenv("AGENT_WORKDIR", str(tmp_path / "work"))
    monkeypatch.setenv("MAX_SHIFTS", "1")
    for var in ("AGENT_MODEL", "AGENT_EFFORT", "AGENT_MODE", "AUTO_UPDATE", "SWARM_END"):
        monkeypatch.delenv(var, raising=False)
    agent = cls.from_env()
    agent.pause_poll_seconds = 0.2
    return agent


def calls(tmp_path) -> list[dict]:
    path = tmp_path / "work" / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


async def run_one_shift(live_server, agent, name):
    runner = Runner(live_server.url, name, agent, step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
    async with anyio.create_task_group() as tg:
        tg.start_soon(runner.run)
        await wait_until(lambda: agent.state.shifts >= 1 and live_server.ws.list_messages(channel="activity"),
                         timeout=20, message="one shift and its report")
        runner.stop()
    tags = set(next(a for a in live_server.ws.list_agents() if a["name"] == name)["skills"])
    return live_server.ws.list_messages(channel="activity")[0]["body"], tags


def test_runner_agent_names_codex_and_agy(monkeypatch):
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    assert type(load_agent("codex")) is CodexAgent and type(load_agent("agy")) is AgyAgent
    assert load_agent("agy").model == "gemini-4-argon-low"


@pytest.mark.anyio
async def test_codex_shift_uses_the_chatgpt_login_and_reports(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text('model = "gpt-test"\nmodel_reasoning_effort = "high"\n')
    agent = make(CodexAgent, tmp_path, monkeypatch, FAKE_CODEX, "CODEX_CMD")
    report, tags = await run_one_shift(live_server, agent, "cx-1")

    [call] = calls(tmp_path)
    args = call["args"]
    assert call["api_key"] is False
    assert args[:2] == ["exec", "--json"] and args[args.index("--sandbox") + 1] == "workspace-write"
    board_url = next(a for a in args if a.startswith("mcp_servers.board.url="))
    assert json.loads(board_url.split("=", 1)[1]).endswith("/mcp")
    assert args[-1].startswith("Be a good swarm agent.") and 'agent_name="cx-1"' in args[-1]
    assert "mcp__board__" not in args[-1] and "--model" not in args  # the Codex config picks the model
    assert report.startswith("Shift 1") and "Codex gpt-test, cost not reported" in report
    assert "did a codex thing" in report
    assert {"model:gpt-test", "effort:high"} <= tags


@pytest.mark.anyio
async def test_codex_usage_limit_backs_off(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LIMIT", "1")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-config"))
    agent = make(CodexAgent, tmp_path, monkeypatch, FAKE_CODEX, "CODEX_CMD")
    agent.model, agent.effort = "gpt-other", "low"  # e.g. from agent.env
    report, tags = await run_one_shift(live_server, agent, "cx-2")
    assert agent.state.blocked_until is not None and "Codex usage limit" in report
    args = calls(tmp_path)[0]["args"]
    assert args[args.index("--model") + 1] == "gpt-other" and 'model_reasoning_effort="low"' in args
    assert {"model:gpt-other", "effort:low"} <= tags


@pytest.mark.anyio
async def test_agy_shift_adds_the_board_to_its_mcp_config(live_server, tmp_path, monkeypatch):
    agent = make(AgyAgent, tmp_path, monkeypatch, FAKE_AGY, "AGY_CMD")
    agent.mcp_config_path = tmp_path / "gemini" / "mcp_config.json"
    agent.mcp_config_path.parent.mkdir()
    agent.mcp_config_path.write_text(json.dumps({"mcpServers": {"other": {"serverUrl": "http://x"}}}))
    report, tags = await run_one_shift(live_server, agent, "agy-t")

    servers = json.loads(agent.mcp_config_path.read_text())["mcpServers"]
    assert servers["other"] == {"serverUrl": "http://x"}  # the owner's servers stay
    assert servers["board"] == {"disabled": False, "serverUrl": live_server.url.rstrip("/") + "/mcp"}
    args = calls(tmp_path)[0]["args"]
    assert args[args.index("--model") + 1] == "gemini-4-argon-low" and args[args.index("--mode") + 1] == "auto"
    assert "--dangerously-skip-permissions" in args and "--effort" not in args
    assert args[args.index("-p") + 1].startswith("Be a good swarm agent.")
    assert report.startswith("Shift 1") and "agy gemini-4-argon-low, cost not reported" in report
    assert "did an agy thing" in report and "looking around" not in report
    assert {"model:gemini-4-argon-low", "effort:default"} <= tags


@pytest.mark.anyio
async def test_agy_quota_error_backs_off(live_server, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LIMIT", "1")
    agent = make(AgyAgent, tmp_path, monkeypatch, FAKE_AGY, "AGY_CMD")
    agent.mcp_config_path = tmp_path / "mcp_config.json"
    report, _ = await run_one_shift(live_server, agent, "agy-limit")
    assert agent.state.blocked_until is not None and "agy quota exceeded" in report
    assert "looking around" in report  # the last thing it said before the error


def test_agy_leaves_an_unreadable_mcp_config_alone(tmp_path):
    agent = AgyAgent(prompt_file=tmp_path / "p.md", workdir=tmp_path / "work")
    agent.mcp_config_path = tmp_path / "mcp_config.json"
    agent.mcp_config_path.write_text("{not json")
    agent._write_mcp_config(types.SimpleNamespace(agent_name="a", client=types.SimpleNamespace(url="http://b/mcp")))
    assert agent.mcp_config_path.read_text() == "{not json"
