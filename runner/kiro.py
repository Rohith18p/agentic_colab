"""Swarm shifts run by the Kiro CLI (`kiro-cli chat --no-interactive`) instead of Claude Code.

Start one with `AGENT_CLI=kiro swarm/start_agent.sh <board-url> <name> [skills]`
(or RUNNER_AGENT=kiro for `python -m runner`). Everything except the CLI call is
shared with runner/claude_code.py: notebook, inbox, pause, SWARM_END, MAX_SHIFTS,
agent.env hot settings, auto-update, shift reports and model/effort tags.

- Shifts use your Kiro login (run `kiro-cli login` once) and its credits.
- The board goes in the agent's own workspace MCP config, <workdir>/.kiro/settings/mcp.json,
  so your global Kiro config (~/.kiro/settings/mcp.json, also loaded) is left alone.
- Shifts run on Kiro's v2 engine: in this version (kiro-cli 2.34) the v3 engine doesn't load
  workspace MCP servers in --no-interactive runs.
- Shifts run with --trust-all-tools, like Claude shifts' allowed tools.
- Kiro reports credits, not dollars, so shift reports give the credits used.

Environment, on top of runner/claude_code.py's:
  KIRO_CMD       the Kiro command (default "kiro-cli")
  AGENT_MODEL    Kiro model (default claude-opus-5.5; `kiro-cli chat --list-models` lists them)
  AGENT_EFFORT   --effort: low, medium, high, xhigh or max (default: Kiro's own)
  KIRO_ENGINE    --agent-engine (default v2)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
from pathlib import Path

from .agent import AgentContext
from .claude_code import MCP_SERVER_NAME, ClaudeCodeAgent, ShiftResult

log = logging.getLogger("runner.kiro")

DEFAULT_MODEL = "claude-opus-5.5"
# Kiro's wording for a used-up allowance or a throttled request. Checked only against error text
# (a failed run's result, or an ERROR line from Kiro itself), never the agent's own output.
LIMIT_RE = re.compile(r"usage limit|limit (?:reached|exceeded)|out of credits|insufficient credits|no credits|"
                      r"quota|too many requests|throttl\w*|rate limit", re.IGNORECASE)


class KiroAgent(ClaudeCodeAgent):
    cli_name = "Kiro"
    api_billing_vars = ()  # Kiro runs on its own login
    board_tools_hint = ""
    engine = "v2"
    _credits = 0.0  # credits used in this shift, from Kiro's metering events

    @classmethod
    def from_env(cls) -> KiroAgent:
        agent = super().from_env()
        agent.command = shlex.split(os.environ.get("KIRO_CMD", "kiro-cli"))
        agent.model = os.environ.get("AGENT_MODEL", DEFAULT_MODEL)
        agent.engine = os.environ.get("KIRO_ENGINE", cls.engine)
        agent.extra_mcp_servers = []
        return agent

    def model_tags(self) -> set[str]:
        return {f"model:{self.model or DEFAULT_MODEL}", f"effort:{self.effort or 'default'}"}

    def _write_mcp_config(self, ctx: AgentContext) -> Path:
        """Point the workspace's Kiro MCP config at the board. Other servers in that file are kept."""
        path = self.workdir(ctx.agent_name) / ".kiro" / "settings" / "mcp.json"
        url = ctx.client.url if ctx.client.url.endswith("/mcp") else ctx.client.url.rstrip("/") + "/mcp"
        self.mcp_servers = [MCP_SERVER_NAME]
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            data = {}
        servers = data.setdefault("mcpServers", {})
        if servers.get(MCP_SERVER_NAME) != {"url": url}:
            servers[MCP_SERVER_NAME] = {"url": url}
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        return path

    def _command(self, ctx: AgentContext, prompt: str) -> list[str]:
        self._write_mcp_config(ctx)
        self._credits = 0.0
        return [
            *self.command, "chat",
            "--no-interactive", "--trust-all-tools", "--trust-workspace",
            "--output-format", "stream-json",
            *(["--agent-engine", self.engine] if self.engine else []),
            *(["--model", self.model] if self.model else []),
            *(["--effort", self.effort] if self.effort else []),
            *self.extra_args,
            self.prompt_file.read_text() + "\n\n" + prompt,
        ]

    def _cost_text(self, result: ShiftResult) -> str:
        return f"Kiro {self.model}, {self._credits:.2f} credits"

    def _parse_event(self, line: bytes, result: ShiftResult) -> None:
        text = line.decode(errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            if "ERROR" in text or text.lower().startswith("error"):
                self._note_limit(text, result)  # Kiro's own log lines on stderr, not agent output
            return
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        kind = event.get("type")
        if kind == "metadata":
            for usage in data.get("meteringUsage") or []:
                if isinstance(usage, dict) and isinstance(usage.get("value"), (int, float)):
                    self._credits += usage["value"]
        elif kind == "sessionUpdate":
            update = data.get("update") or {}
            if update.get("sessionUpdate") == "agent_message_chunk":
                chunk = (update.get("content") or {}).get("text")
                if isinstance(chunk, str):
                    result.summary = ((result.summary or "") + chunk)[-2000:]
        elif kind == "runFinished":
            if isinstance(data.get("finalText"), str) and data["finalText"].strip():
                result.summary = data["finalText"][-2000:]
            if data.get("status") not in (None, "success"):
                self._note_limit(json.dumps({k: data.get(k) for k in ("status", "stopReason", "error", "finalText")}),
                                 result)
        elif kind in ("error", "runError"):
            self._note_limit(text, result)

    @staticmethod
    def _note_limit(text: str, result: ShiftResult) -> None:
        match = LIMIT_RE.search(text)
        if match and result.limit is None:
            result.limit = f"Kiro {match.group(0)}"
