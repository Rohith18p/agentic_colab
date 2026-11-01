"""Swarm shifts run by the OpenAI Codex CLI (`codex exec`) instead of Claude Code.

Start one with `AGENT_CLI=codex swarm/start_agent.sh <board-url> <name> [skills]`
(or RUNNER_AGENT=codex for `python -m runner`). Everything except the CLI call is
shared with runner/claude_code.py: notebook, inbox, pause, SWARM_END, MAX_SHIFTS,
agent.env hot settings, auto-update, shift reports and model/effort tags.

- Shifts use your `codex login` (ChatGPT plan). OPENAI_API_KEY and CODEX_API_KEY are
  removed from every shift unless ALLOW_API_BILLING=1.
- The board is attached per shift with `-c mcp_servers.board.*` overrides, so your Codex
  config is not changed. Other MCP servers (alphaxiv, Hugging Face) come from your own
  ~/.codex/config.toml if you added them there.
- Codex doesn't report a dollar cost, so shift reports say "cost not reported".

Environment, on top of runner/claude_code.py's:
  CODEX_CMD      the codex command (default "codex")
  AGENT_MODEL    Codex model (default: `model` in your Codex config)
  AGENT_EFFORT   model_reasoning_effort (default: your Codex config)
  CODEX_SANDBOX  Codex sandbox mode (default "workspace-write", with network access)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
from pathlib import Path

from .agent import AgentContext
from .claude_code import MCP_SERVER_NAME, ClaudeCodeAgent, ShiftResult

# Codex's own messages when a plan allowance or rate limit is hit.
LIMIT_RE = re.compile(r"usage limit|rate limit|quota exceeded|too many requests", re.IGNORECASE)


def codex_config() -> dict:
    """Top-level settings from ~/.codex/config.toml (or $CODEX_HOME/config.toml); {} if unreadable."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    try:
        return tomllib.loads((home / "config.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


class CodexAgent(ClaudeCodeAgent):
    cli_name = "Codex"
    api_billing_vars = ("OPENAI_API_KEY", "CODEX_API_KEY")
    board_tools_hint = ""
    sandbox = "workspace-write"

    @classmethod
    def from_env(cls) -> CodexAgent:
        agent = super().from_env()
        agent.command = shlex.split(os.environ.get("CODEX_CMD", "codex"))
        agent.model = os.environ.get("AGENT_MODEL", "")
        agent.sandbox = os.environ.get("CODEX_SANDBOX", cls.sandbox)
        agent.extra_mcp_servers = []  # research servers come from the Codex config instead
        return agent

    def model_tags(self) -> set[str]:
        config = codex_config()
        model = self.model or config.get("model") or "default"
        effort = self.effort or config.get("model_reasoning_effort") or "default"
        return {f"model:{model}", f"effort:{effort}"}

    def _write_mcp_config(self, ctx: AgentContext) -> None:
        self.mcp_servers = [MCP_SERVER_NAME]  # attached with -c overrides in _command

    def _command(self, ctx: AgentContext, prompt: str) -> list[str]:
        self._write_mcp_config(ctx)
        board = f"mcp_servers.{MCP_SERVER_NAME}"
        network = ["-c", "sandbox_workspace_write.network_access=true"] if self.sandbox == "workspace-write" else []
        return [
            *self.command, "exec", "--json", "--skip-git-repo-check",
            "--sandbox", self.sandbox, *network,
            "-c", f"{board}.url={json.dumps(ctx.client.url)}",
            "-c", f'{board}.default_tools_approval_mode="approve"',
            "-c", f"{board}.required=true",
            *(["--model", self.model] if self.model else []),
            *(["-c", f"model_reasoning_effort={json.dumps(self.effort)}"] if self.effort else []),
            *self.extra_args,
            self.prompt_file.read_text() + "\n\n" + prompt,
        ]

    def _cost_text(self, result: ShiftResult) -> str:
        model = self.model or codex_config().get("model")
        return f"Codex{' ' + model if model else ''}, cost not reported"

    @staticmethod
    def _parse_event(line: bytes, result: ShiftResult) -> None:
        text = line.decode(errors="replace").strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            event = None
        if not isinstance(event, dict) or event.get("type") in ("error", "turn.failed"):
            # Codex's own stderr or error events (not agent output, so quoting a limit can't trigger it).
            match = LIMIT_RE.search(text)
            if match and result.limit is None:
                result.limit = f"Codex {match.group(0)}"
            return
        if event.get("type") == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                result.summary = item["text"][-2000:]
            error = (item.get("error") or {}).get("message", "")
            if item.get("type") == "mcp_tool_call" and item.get("server") == MCP_SERVER_NAME \
                    and "requires approval" in error and result.limit is None:
                result.limit = "board MCP tool approval denied"
