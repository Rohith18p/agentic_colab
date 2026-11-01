"""Run an agent next to the workspace: `python -m runner`.

Environment:
  WORKSPACE_URL   e.g. http://localhost:8000 (required)
  AGENT_NAME      unique agent name (required)
  AGENT_OWNER     the human running this agent (optional)
  RUNNER_AGENT    "echo" (default), "claude" (Claude Code shifts; see runner/claude_code.py),
                  "codex" (runner/codex.py), "agy" (Antigravity; runner/agy.py)
                  or "package.module:ClassName" for your own Agent subclass
  STEP_SECONDS    pause between steps (default 1.5)
  AGENT_MACHINE   how this machine is shown on the board (default: hostname and GPUs)
"""

from __future__ import annotations

import importlib
import logging
import os
import sys

import anyio

from .agent import Agent
from .claude_code import ClaudeCodeAgent, describe_machine
from .echo import EchoAgent
from .runner import Runner


def load_agent(spec: str) -> Agent:
    if spec == "echo":
        return EchoAgent()
    if spec == "claude":
        return ClaudeCodeAgent.from_env()
    if spec == "codex":  # imported only when used, so a Claude agent never depends on them
        from .codex import CodexAgent

        return CodexAgent.from_env()
    if spec == "agy":
        from .agy import AgyAgent

        return AgyAgent.from_env()
    module_name, _, class_name = spec.partition(":")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpx2", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    url = os.environ.get("WORKSPACE_URL")
    name = os.environ.get("AGENT_NAME")
    if not url or not name:
        sys.exit("Set WORKSPACE_URL and AGENT_NAME.")
    step = float(os.environ.get("STEP_SECONDS", "1.5"))
    runner = Runner(
        url,
        name,
        load_agent(os.environ.get("RUNNER_AGENT", "echo")),
        owner_name=os.environ.get("AGENT_OWNER", ""),
        machine=os.environ.get("AGENT_MACHINE") or describe_machine(),
        step_interval=step,
        idle_interval=max(step, 2.0),
    )
    try:
        anyio.run(runner.run)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
