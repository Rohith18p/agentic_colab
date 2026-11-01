"""Swarm shifts run by the Google Antigravity CLI (`agy -p`) instead of Claude Code.

Start one with `AGENT_CLI=agy swarm/start_agent.sh <board-url> <name> [skills]`
(or RUNNER_AGENT=agy for `python -m runner`). Everything except the CLI call is
shared with runner/claude_code.py: notebook, inbox, pause, SWARM_END, MAX_SHIFTS,
agent.env hot settings, auto-update, shift reports and model/effort tags.

- Shifts use your Antigravity login (run `agy` once to log in).
- agy reads MCP servers only from its user config, so the runner adds (or updates) one
  `board` entry in ~/.gemini/config/mcp_config.json and leaves your other servers alone.
- Shifts run with --dangerously-skip-permissions, like Claude shifts' allowed tools.
- agy doesn't report a dollar cost, so shift reports say "cost not reported".

Environment, on top of runner/claude_code.py's:
  AGY_CMD        the agy command (default "agy")
  AGENT_MODEL    agy model (default gemini-4-argon-low; `agy models` lists them)
  AGENT_EFFORT   --effort: low, medium, high, xhigh or max (default: agy's own)
  AGENT_MODE     --mode: auto (default), accept-edits or plan
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

log = logging.getLogger("runner.agy")

DEFAULT_MODEL = "gemini-4-argon-low"
# agy's own messages when a plan allowance or rate limit is hit.
LIMIT_RE = re.compile(r"usage limit|rate limit|quota exceeded|too many requests|resource exhausted", re.IGNORECASE)


class AgyAgent(ClaudeCodeAgent):
    cli_name = "agy"
    api_billing_vars = ()  # agy runs on the Antigravity login
    board_tools_hint = ""
    mode = "auto"
    mcp_config_path: Path | None = None  # default ~/.gemini/config/mcp_config.json
    _response = ""  # text of the latest agent response in this shift
    _response_step: int | None = None

    @classmethod
    def from_env(cls) -> AgyAgent:
        agent = super().from_env()
        agent.command = shlex.split(os.environ.get("AGY_CMD", "agy"))
        agent.model = os.environ.get("AGENT_MODEL", DEFAULT_MODEL)
        agent.mode = os.environ.get("AGENT_MODE", cls.mode)
        agent.extra_mcp_servers = []
        return agent

    def model_tags(self) -> set[str]:
        return {f"model:{self.model or DEFAULT_MODEL}", f"effort:{self.effort or 'default'}"}

    def _write_mcp_config(self, ctx: AgentContext) -> Path:
        """Add the board to agy's MCP config if it isn't there yet. Other servers are kept."""
        path = self.mcp_config_path or Path.home() / ".gemini" / "config" / "mcp_config.json"
        url = ctx.client.url if ctx.client.url.endswith("/mcp") else ctx.client.url.rstrip("/") + "/mcp"
        entry = {"disabled": False, "serverUrl": url}
        self.mcp_servers = [MCP_SERVER_NAME]
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
            log.warning("%s isn't a readable agy MCP config, so it was left alone. Add the board once with: "
                        "agy mcp add %s %s", path, MCP_SERVER_NAME, url)
            return path
        servers = data.setdefault("mcpServers", {})
        if servers.get(MCP_SERVER_NAME) != entry:
            servers[MCP_SERVER_NAME] = entry
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        return path

    def _command(self, ctx: AgentContext, prompt: str) -> list[str]:
        self._write_mcp_config(ctx)
        self._response, self._response_step = "", None
        return [
            *self.command,
            "-p", self.prompt_file.read_text() + "\n\n" + prompt,
            "--output-format", "stream-json",
            "--dangerously-skip-permissions",
            *(["--model", self.model] if self.model else []),
            *(["--mode", self.mode] if self.mode else []),
            *(["--effort", self.effort] if self.effort else []),
            *self.extra_args,
        ]

    def _cost_text(self, result: ShiftResult) -> str:
        return f"agy {self.model}, cost not reported"

    def _parse_event(self, line: bytes, result: ShiftResult) -> None:
        text = line.decode(errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            event = None
        if not isinstance(event, dict) or event.get("event") == "error":
            self._note_limit(text, result)  # agy's own stderr or error events, not agent output
            return
        if event.get("event") == "step_update":
            step = event.get("step_update") or {}
            if step.get("step_type") == "agent_response" and isinstance(step.get("text_delta"), str):
                if step.get("step_index") != self._response_step:
                    self._response, self._response_step = "", step.get("step_index")
                self._response += step["text_delta"]
                if self._response.strip():
                    result.summary = self._response[-2000:]  # the latest response, until the final result
        elif event.get("event") == "result":
            res = event.get("result") or {}
            if isinstance(res.get("response"), str) and res["response"].strip():
                result.summary = res["response"][-2000:]
            if res.get("status") not in (None, "SUCCESS"):
                self._note_limit(json.dumps({k: res.get(k) for k in ("status", "error")}), result)

    @staticmethod
    def _note_limit(text: str, result: ShiftResult) -> None:
        match = LIMIT_RE.search(text)
        if match and result.limit is None:
            result.limit = f"agy {match.group(0)}"
