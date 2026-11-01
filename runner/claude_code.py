"""ClaudeCodeAgent: each runner step is one headless Claude Code "shift".

A shift is one `claude -p` run with the workspace attached as the "board" MCP server
and the swarm playbook appended to the system prompt. Claude does one unit of
work through the workspace tools and exits. The board (plus a NOTEBOOK.md in
the agent's working directory) is its only memory between shifts.

While a shift runs, the Runner keeps heartbeating, so task leases stay alive.
A separate watcher polls the pause state and stops the shift when a human
pauses the agent. The runner then waits until the agent is resumed.

Hot reload between shifts:
- the playbook file is re-read for every shift;
- <workdir>/agent.env (KEY=VALUE lines) is re-read before every shift and
  overrides the settings below, so an owner can change the model or budgets
  without restarting;
- with AUTO_UPDATE=1 the checkout is fast-forwarded (`git pull --ff-only`)
  before every shift, and the runner restarts itself (same process slot, state
  kept in the work directory) when files under runner/ changed.

Environment (all optional except what the Runner needs):
  AGENT_CMD            command that runs Claude Code (default "claude")
  AGENT_MODEL          model for every shift (default "claude-opus-5-5")
  AGENT_EFFORT         Claude Code --effort for every shift: low, medium, high, xhigh or max (default:
                       unset, so Claude Code uses the machine's own setting, e.g. effortLevel in
                       ~/.claude/settings.json). The agent's board tags show both, as model:<model>
                       and effort:<level>, and follow changes.
  AGENT_PROMPT_FILE    playbook appended to the system prompt (default swarm/AGENT_PROMPT.md)
  AGENT_WORKDIR        persistent working directory (default ~/swarm-work/<AGENT_NAME>)
  AGENT_SKILLS         comma-separated machine capabilities, e.g. "cosmos-gpu,isaac-sim"
  (No spend caps: shifts run until they finish or hit SHIFT_TIMEOUT_SECONDS. SHIFT_BUDGET_USD and
  TOTAL_BUDGET_USD in the environment are ignored. The plan's usage limit is the only brake; see
  LIMIT_BACKOFF_SECONDS.)
  MAX_SHIFTS           run this many shifts, then exit the runner (counted per start, so
                       MAX_SHIFTS=1 is a one-shift smoke test; default: no limit)
  SHIFT_TIMEOUT_SECONDS  stop a shift that runs longer than this (default 2700)
  PERMISSION_MODE      Claude Code --permission-mode (default "default")
  ALLOWED_TOOLS        Claude Code --allowedTools (default: shell, files, web, board); every attached
                       MCP server's tools are added to it
  EXTRA_MCP_SERVERS    comma-separated MCP servers from this machine's Claude Code config that every
                       shift also gets (default "alphaxiv,hf-mcp-server": papers and the Hugging Face
                       Hub). Their definitions are copied, so shifts reuse the OAuth login done once
                       in an interactive session (`claude`, then /mcp). Missing ones are skipped.
  AGENT_EXTRA_ARGS     extra Claude Code arguments, shell-quoted
  AUTO_UPDATE          "1" to pull the repo before each shift and restart on runner/ changes
  SWARM_END            ISO time with offset (e.g. 2026-11-01T08:00:00-05:00): stop any running
                       shift, start no more, and exit the runner
  ON_MENTION           "1": start a shift only when there are new messages for this agent
                       (for the explainer: it costs nothing while nobody asks)
  SHIFT_REPORTS        "0" to not post a report to #activity after each shift (default "1")
  ALLOW_API_BILLING    "1" to let shifts use ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN. By default
                       both are removed from each shift's environment, so Claude Code uses the
                       `claude` login (a Pro/Max/Team/Enterprise plan's included usage) and never
                       bills API credits. In `claude -p`, a key in the environment always wins.
  LIMIT_BACKOFF_SECONDS  after a plan usage-limit or spend-limit error, wait this long before the
                       next shift instead of retrying (default 1800)
  LIMIT_RELEASE_SECONDS  when usage limits will keep the agent from working for at least this long
                       (counting the current backoff), it releases its claimed tasks with a hand-off
                       note so others can take them (default 1800, so the first limit releases;
                       "0" never releases). It can reclaim them later if they are still open.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import anyio

from .agent import Agent, AgentContext
from .client import ToolCallError, WorkspaceClient, WorkspaceUnreachable

log = logging.getLogger("runner.claude_code")

REPO_ROOT = Path(__file__).resolve().parent.parent
# Claude Code reserves the MCP server name "workspace", so the board is attached as "board".
MCP_SERVER_NAME = "board"
DEFAULT_TOOLS = f"Bash Read Edit Write Glob Grep WebFetch WebSearch mcp__{MCP_SERVER_NAME}"
DEFAULT_EXTRA_MCP = "alphaxiv,hf-mcp-server"
# How a teammate adds each default research server once (then logs in with /mcp in `claude`).
EXTRA_MCP_SETUP = {
    "alphaxiv": "claude mcp add --scope user --transport http alphaxiv https://api.alphaxiv.org/mcp/v1",
    "hf-mcp-server": "claude mcp add --scope user --transport http hf-mcp-server 'https://huggingface.co/mcp?login'",
}
# alphaxiv tools that change the owner's personal library or follows; shifts never call them.
DISALLOWED_TOOLS = " ".join(f"mcp__alphaxiv__{tool}" for tool in (
    "create_folder", "delete_folder", "rename_folder", "save_papers_to_folder", "remove_papers_from_folder",
    "move_papers_between_folders", "edit_private_paper_metadata", "follow_researcher", "unfollow_researcher",
))
# Credentials that make Claude Code bill API usage instead of the logged-in plan.
API_BILLING_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
# Claude Code's messages when a plan allowance or a usage-credit spend limit is reached
# (https://code.claude.com/docs/en/errors). Retrying before the reset only burns attempts.
# Any "You've hit your ... limit/budget" counts, so a new wording (e.g. "5-hour limit") isn't retried
# every STEP_SECONDS while the agent keeps holding its tasks.
LIMIT_RE = re.compile(
    r"You[’']ve hit your [^\n.·]{0,60}?(?:limit|budget)"
    r"|Credit balance is too low|spend limit reached",
    re.IGNORECASE,
)

NOTEBOOK_TEMPLATE = """\
# Notebook for {name}

Read this at the start of every shift and update it at the end. It is your
only memory besides the board.

## Doing now

## Jobs running (PID, log path, what to check)

## Next steps

## Things I learned (with links to board messages)
"""


@dataclass
class ShiftResult:
    exit_code: int | None = None
    cost_usd: float = 0.0
    stopped: str | None = None  # "paused", "timeout" or "swarm_end" when the harness stopped it
    summary: str = ""
    limit: str | None = None  # a usage/spend-limit message Claude Code reported, if any
    reset_seconds: float | None = None  # when the CLI said how long until the limit resets


def note_limit(result: ShiftResult, text: str) -> None:
    if result.limit is None:
        match = LIMIT_RE.search(text)
        if match:
            result.limit = match.group(0)


@dataclass
class HarnessState:
    inbox_cursor: int = 0
    spent_usd: float = 0.0
    shifts: int = 0
    last_stop: str | None = None
    blocked_until: str | None = None  # ISO time; no shifts before it (after a usage limit)
    limited_since: str | None = None  # ISO time of the first usage limit in the current streak
    history: list[dict[str, Any]] = field(default_factory=list)


def _env_float(name: str, default: float | None) -> float | None:
    value = os.environ.get(name)
    return float(value) if value else default


def _names(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _optional(parse):
    return lambda value: parse(value) if value else None


def parse_end(value: str | None) -> datetime | None:
    """SWARM_END as an aware datetime; it must carry a UTC offset."""
    if not value:
        return None
    end = datetime.fromisoformat(value)
    if end.tzinfo is None:
        raise ValueError(f"SWARM_END needs a UTC offset, e.g. 2026-11-01T08:00:00-05:00 (got {value!r})")
    return end


# Settings that agent.env may change between shifts: env name -> (attribute, parser).
HOT_SETTINGS = {
    "AGENT_MODEL": ("model", str),
    "AGENT_EFFORT": ("effort", _optional(str)),
    "MAX_SHIFTS": ("max_shifts", _optional(int)),
    "SHIFT_TIMEOUT_SECONDS": ("shift_timeout_seconds", float),
    "PERMISSION_MODE": ("permission_mode", str),
    "ALLOWED_TOOLS": ("allowed_tools", str),
    "AGENT_EXTRA_ARGS": ("extra_args", shlex.split),
    "SWARM_END": ("end", parse_end),
    "EXTRA_MCP_SERVERS": ("extra_mcp_servers", _names),
}


def claude_config_file() -> Path:
    """Where Claude Code keeps user-scope MCP servers (`claude mcp add --scope user`)."""
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base).expanduser() / ".claude.json" if base else Path.home() / ".claude.json"


def claude_settings_effort() -> str | None:
    """effortLevel from Claude Code's user settings, which headless shifts use when --effort isn't given."""
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    try:
        value = json.loads((base / "settings.json").read_text()).get("effortLevel")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def user_mcp_servers(path: Path) -> dict[str, dict]:
    try:
        servers = json.loads(path.read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return servers if isinstance(servers, dict) else {}


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments are ignored, surrounding quotes stripped."""
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        values[key] = value.strip().strip("'\"")
    return values


def describe_machine() -> str:
    """Hostname plus GPUs, e.g. "box · 1x NVIDIA H100 80GB HBM3 (81559 MiB)", shown on the board."""
    host = socket.gethostname()
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    gpus = Counter(" (".join(part.strip() for part in line.split(",", 1)) + ")"
                   for line in out.splitlines() if line.strip())
    if not gpus:
        return f"{host} · no GPU"
    return f"{host} · " + ", ".join(f"{n}x {gpu}" for gpu, n in gpus.items())


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=60)


def git_head(repo: Path) -> str | None:
    try:
        return _git(repo, "rev-parse", "HEAD").stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def self_update(repo: Path, loaded: str | None = None) -> bool:
    """Fast-forward the checkout. Returns True if files under runner/ differ from `loaded`, the commit
    this process started from (default: HEAD before the pull). Comparing with `loaded` catches updates
    that someone else pulled into a shared checkout, e.g. start.sh starting a second agent."""
    before = git_head(repo)
    pulled = _git(repo, "pull", "--ff-only", "-q")
    if pulled.returncode != 0:
        log.warning("auto-update: git pull failed, carrying on: %s", (pulled.stderr or pulled.stdout).strip()[:300])
    after = git_head(repo)
    before = loaded or before
    if not before or not after or before == after:
        return False
    changed = _git(repo, "diff", "--name-only", before, after, "--", "runner/").stdout.split()
    log.info("auto-update: %s -> %s%s", before[:8], after[:8], f" (runner changed: {changed})" if changed else "")
    return bool(changed)


class ClaudeCodeAgent(Agent):
    # What other agent CLIs (runner/codex.py, runner/agy.py) change; the shift lifecycle is shared.
    cli_name = "Claude"
    api_billing_vars: tuple[str, ...] = API_BILLING_VARS  # removed from every shift's environment
    board_tools_hint = f" (tools named mcp__{MCP_SERVER_NAME}__*)"

    def __init__(
        self,
        *,
        command: list[str] | None = None,
        model: str = "claude-opus-5-5",
        prompt_file: Path | str | None = None,
        workdir: Path | str | None = None,
        skills: list[str] | None = None,
        shift_budget_usd: float | None = None,
        total_budget_usd: float | None = None,
        max_shifts: int | None = None,
        shift_timeout_seconds: float = 2700,
        permission_mode: str = "default",
        allowed_tools: str = DEFAULT_TOOLS,
        extra_args: list[str] | None = None,
        pause_poll_seconds: float = 10.0,
        auto_update: bool = False,
        repo_root: Path | str | None = None,
        end: datetime | None = None,
        on_mention: bool = False,
        shift_reports: bool = True,
        allow_api_billing: bool = False,
        limit_backoff_seconds: float = 1800,
        limit_release_seconds: float = 1800,
        extra_mcp_servers: list[str] | None = None,
        claude_config: Path | str | None = None,
        effort: str | None = None,
    ) -> None:
        self.command = command or ["claude"]
        self.model = model
        self.prompt_file = Path(prompt_file) if prompt_file else REPO_ROOT / "swarm" / "AGENT_PROMPT.md"
        self._workdir = Path(workdir).expanduser() if workdir else None
        self.skills = list(skills or [])
        self.shift_budget_usd = shift_budget_usd
        self.total_budget_usd = total_budget_usd
        self.max_shifts = max_shifts
        self.shift_timeout_seconds = shift_timeout_seconds
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.extra_args = list(extra_args or [])
        self.pause_poll_seconds = pause_poll_seconds
        self.auto_update = auto_update
        self.repo_root = Path(repo_root) if repo_root else REPO_ROOT
        self.end = end
        self.on_mention = on_mention
        self.shift_reports = shift_reports
        self.allow_api_billing = allow_api_billing
        self.limit_backoff_seconds = limit_backoff_seconds
        self.limit_release_seconds = limit_release_seconds
        self.extra_mcp_servers = _names(DEFAULT_EXTRA_MCP) if extra_mcp_servers is None else list(extra_mcp_servers)
        self.claude_config = Path(claude_config) if claude_config else claude_config_file()
        self.mcp_servers: list[str] = [MCP_SERVER_NAME]  # attached to the latest shift
        self.effort = effort
        self._tags_synced: set[str] | None = None  # model:/effort: tags last written to the board
        self._missing_mcp_warned: set[str] = set()
        self.done = False  # set at SWARM_END; the Runner then stops
        self.state = HarnessState()
        self._announced_stop = False
        self._env_file_mtime: float | None = None
        self._loaded_head: str | None = None  # commit this process's code came from (AUTO_UPDATE)
        # Carried across self-update restarts (os.execv), which continue the same run.
        self._shifts_this_run = int(os.environ.pop("RUNNER_SHIFTS_THIS_RUN", "") or 0)

    @classmethod
    def from_env(cls) -> ClaudeCodeAgent:
        max_shifts = os.environ.get("MAX_SHIFTS")
        return cls(
            command=shlex.split(os.environ.get("AGENT_CMD", "claude")),
            model=os.environ.get("AGENT_MODEL", "claude-opus-5-5"),
            prompt_file=os.environ.get("AGENT_PROMPT_FILE") or None,
            workdir=os.environ.get("AGENT_WORKDIR") or None,
            skills=[s.strip() for s in os.environ.get("AGENT_SKILLS", "").split(",") if s.strip()],
            max_shifts=int(max_shifts) if max_shifts else None,
            shift_timeout_seconds=_env_float("SHIFT_TIMEOUT_SECONDS", 2700),
            permission_mode=os.environ.get("PERMISSION_MODE", "default"),
            allowed_tools=os.environ.get("ALLOWED_TOOLS", DEFAULT_TOOLS),
            extra_args=shlex.split(os.environ.get("AGENT_EXTRA_ARGS", "")),
            auto_update=os.environ.get("AUTO_UPDATE") == "1",
            end=parse_end(os.environ.get("SWARM_END")),
            on_mention=os.environ.get("ON_MENTION") == "1",
            shift_reports=os.environ.get("SHIFT_REPORTS", "1") != "0",
            allow_api_billing=os.environ.get("ALLOW_API_BILLING") == "1",
            limit_backoff_seconds=_env_float("LIMIT_BACKOFF_SECONDS", 1800),
            limit_release_seconds=_env_float("LIMIT_RELEASE_SECONDS", 1800),
            extra_mcp_servers=_names(os.environ.get("EXTRA_MCP_SERVERS", DEFAULT_EXTRA_MCP)),
            effort=os.environ.get("AGENT_EFFORT") or None,
        )

    # ------------------------------------------------------------------ files

    def workdir(self, agent_name: str) -> Path:
        return self._workdir or Path.home() / "swarm-work" / agent_name

    def _state_path(self, agent_name: str) -> Path:
        return self.workdir(agent_name) / ".harness_state.json"

    def _load_state(self, agent_name: str) -> None:
        path = self._state_path(agent_name)
        if path.exists():
            data = json.loads(path.read_text())
            self.state = HarnessState(**{k: v for k, v in data.items() if k in HarnessState.__dataclass_fields__})

    def _save_state(self, agent_name: str) -> None:
        self._state_path(agent_name).write_text(json.dumps(self.state.__dict__, indent=2))

    def _write_mcp_config(self, ctx: AgentContext) -> Path:
        """The board plus the research servers, copied from this machine's Claude Code config."""
        servers: dict[str, Any] = {MCP_SERVER_NAME: {"type": "http", "url": ctx.client.url}}
        configured = user_mcp_servers(self.claude_config) if self.extra_mcp_servers else {}
        for name in self.extra_mcp_servers:
            if name in configured and name != MCP_SERVER_NAME:
                servers[name] = configured[name]
            elif name not in self._missing_mcp_warned:
                self._missing_mcp_warned.add(name)
                setup = EXTRA_MCP_SETUP.get(name, f"claude mcp add --scope user {name} ...")
                log.warning("MCP server %r is not in %s, so shifts run without it. Add it once with: %s "
                            "(then run `claude` and log in under /mcp)", name, self.claude_config, setup)
        self.mcp_servers = list(servers)
        path = self.workdir(ctx.agent_name) / ".mcp-workspace.json"
        path.write_text(json.dumps({"mcpServers": servers}, indent=2))
        path.chmod(0o600)  # copied definitions can carry tokens in headers
        return path

    def effective_effort(self) -> str:
        return self.effort or claude_settings_effort() or "default"

    def model_tags(self) -> set[str]:
        return {f"model:{self.model.removeprefix('claude-')}", f"effort:{self.effective_effort()}"}

    async def _sync_tags(self, ctx: AgentContext) -> None:
        """Keep this agent's model:/effort: tags on the board in step with its settings."""
        want = self.model_tags()
        if want == self._tags_synced:
            return
        try:
            agents = (await ctx.call("list_agents"))["agents"]
            have = set(next((a.get("skills") or [] for a in agents if a["name"] == ctx.agent_name), []))
            stale = sorted(t for t in have if t.startswith(("model:", "effort:")) and t not in want)
            await ctx.call("update_skills", agent_name=ctx.agent_name, add=sorted(want - have), remove=stale)
            self._tags_synced = want
        except (ToolCallError, WorkspaceUnreachable, KeyError) as e:
            log.warning("%s: couldn't update model/effort tags: %s", ctx.agent_name, e)

    def _allowed_tools(self) -> str:
        tools = self.allowed_tools.split()
        return " ".join(tools + [f"mcp__{name}" for name in self.mcp_servers if f"mcp__{name}" not in tools])

    # ------------------------------------------------------------------ agent hooks

    async def setup(self, ctx: AgentContext) -> None:
        if self.auto_update:
            self._loaded_head = git_head(self.repo_root)
        wd = self.workdir(ctx.agent_name)
        (wd / "logs").mkdir(parents=True, exist_ok=True)
        notebook = wd / "NOTEBOOK.md"
        if not notebook.exists():
            notebook.write_text(NOTEBOOK_TEMPLATE.format(name=ctx.agent_name))
        if not self.prompt_file.exists():
            raise FileNotFoundError(f"playbook not found: {self.prompt_file} (set AGENT_PROMPT_FILE)")
        self._load_state(ctx.agent_name)
        ctx.inbox_cursor = self.state.inbox_cursor
        self._write_mcp_config(ctx)
        log.info("%s: workdir %s, %d shifts so far, $%.2f spent", ctx.agent_name, wd, self.state.shifts,
                 self.state.spent_usd)

    def _reload_settings(self, agent_name: str) -> None:
        """Apply <workdir>/agent.env if it changed since the last shift."""
        path = self.workdir(agent_name) / "agent.env"
        mtime = path.stat().st_mtime if path.exists() else None
        if mtime == self._env_file_mtime:
            return
        self._env_file_mtime = mtime
        if mtime is None:
            return
        changed = {}
        for key, value in read_env_file(path).items():
            if key not in HOT_SETTINGS:
                continue
            attr, parse = HOT_SETTINGS[key]
            try:
                new = parse(value)
            except ValueError:
                log.warning("%s: ignoring %s=%r in %s", agent_name, key, value, path)  # e.g. a bad SWARM_END
                continue
            if getattr(self, attr) != new:
                setattr(self, attr, new)
                changed[key] = value
        if changed:
            log.info("%s: settings from agent.env: %s", agent_name, changed)
            self._announced_stop = False  # a raised limit lets the agent start shifts again

    def _past_end(self) -> bool:
        return self.end is not None and datetime.now(timezone.utc) >= self.end

    def _out_of_budget(self) -> str | None:
        if self.total_budget_usd is not None and self.state.spent_usd >= self.total_budget_usd:
            return f"spent ${self.state.spent_usd:.2f} of TOTAL_BUDGET_USD=${self.total_budget_usd:.2f}"
        return None

    async def step(self, ctx: AgentContext) -> bool:
        if self.max_shifts is not None and self._shifts_this_run >= self.max_shifts:
            log.info("%s: ran MAX_SHIFTS=%d; exiting", ctx.agent_name, self.max_shifts)
            await ctx.post_message(f"{ctx.agent_name} ran its {self.max_shifts} shift(s) (MAX_SHIFTS) and has "
                                   f"stopped. Its owner can start it again; it keeps its notebook.")
            self.done = True
            return False
        if self.auto_update and await anyio.to_thread.run_sync(self_update, self.repo_root, self._loaded_head):
            log.info("%s: runner code changed; restarting the runner", ctx.agent_name)
            os.environ["RUNNER_SHIFTS_THIS_RUN"] = str(self._shifts_this_run)
            os.execv(sys.executable, [sys.executable, "-m", "runner"])
        self._reload_settings(ctx.agent_name)
        if self._past_end():
            log.warning("%s: SWARM_END (%s) has passed; stopping for good", ctx.agent_name, self.end.isoformat())
            self.done = True
            return False
        await self._sync_tags(ctx)
        if self.state.blocked_until and datetime.now(timezone.utc) < datetime.fromisoformat(self.state.blocked_until):
            return False  # waiting out a plan usage limit
        reason = self._out_of_budget()
        if reason:
            if not self._announced_stop:
                self._announced_stop = True
                log.warning("%s: not starting more shifts: %s", ctx.agent_name, reason)
                await ctx.post_message(
                    f"{ctx.agent_name} has stopped starting new shifts ({reason}). Its owner can raise "
                    f"the limit in {self.workdir(ctx.agent_name) / 'agent.env'}; no restart needed."
                )
            return False

        old_cursor = ctx.inbox_cursor
        inbox = await ctx.read_inbox()
        if self.on_mention and not inbox:
            return False  # nobody asked anything; check again after the idle interval
        number = self.state.shifts + 1
        started = datetime.now(timezone.utc)
        prompt = self._shift_prompt(ctx, number, inbox)
        log_path = self.workdir(ctx.agent_name) / "logs" / f"shift-{number:04d}.jsonl"
        log.info("%s: starting shift %d (%d new messages)", ctx.agent_name, number, len(inbox))
        result = await self._run_shift(ctx, self._command(ctx, prompt), log_path)

        if result.exit_code not in (0, None) and result.stopped is None and result.cost_usd == 0:
            # The shift failed before doing anything; let the next shift see these messages again.
            ctx.inbox_cursor = old_cursor
        self.state.inbox_cursor = ctx.inbox_cursor
        self.state.shifts = number
        self._shifts_this_run += 1
        self.state.spent_usd += result.cost_usd
        self.state.last_stop = result.stopped
        if result.limit:
            now = datetime.now(timezone.utc)
            # Wait as long as the CLI says the limit lasts (plus a margin), else the default backoff.
            wait = result.reset_seconds + 30 if result.reset_seconds is not None else self.limit_backoff_seconds
            until = now + timedelta(seconds=wait)
            self.state.blocked_until = until.isoformat(timespec="seconds")
            self.state.limited_since = self.state.limited_since or now.isoformat(timespec="seconds")
            log.warning("%s: %s; no shifts until %s", ctx.agent_name, result.limit, self.state.blocked_until)
            if self.limit_release_seconds and (
                    until - datetime.fromisoformat(self.state.limited_since)).total_seconds() >= self.limit_release_seconds:
                await self._release_held(ctx, result.limit)
        else:
            self.state.blocked_until = None
            self.state.limited_since = None
        self.state.history = (self.state.history + [{
            "shift": number,
            "ended_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "exit_code": result.exit_code,
            "cost_usd": round(result.cost_usd, 4),
            "stopped": result.stopped,
            "log": str(log_path),
        }])[-50:]
        self._save_state(ctx.agent_name)
        log.info("%s: shift %d ended (exit %s, $%.2f, stopped=%s); total $%.2f", ctx.agent_name, number,
                 result.exit_code, result.cost_usd, result.stopped, self.state.spent_usd)
        if self.shift_reports:
            await self._post_report(ctx, number, started, result)
        return True

    async def _release_held(self, ctx: AgentContext, limit: str) -> None:
        """Hand back claimed tasks while a usage limit keeps this agent from working on them.

        Heartbeats keep leases alive even while no shift can run, so without this a rate-limited
        agent would hold its tasks for hours."""
        try:
            held = [t for t in await ctx.list_tasks(claimed_by=ctx.agent_name)
                    if t.get("status") in ("claimed", "in_progress")]
        except Exception as e:  # board hiccup: try again after the next limited shift
            log.warning("%s: couldn't list held tasks to release them: %s", ctx.agent_name, e)
            return
        notebook = self.workdir(ctx.agent_name) / "NOTEBOOK.md"
        for t in held:
            reason = (f"released automatically: {ctx.agent_name} is held up by a usage limit ({limit}) until at "
                      f"least {self.state.blocked_until}. Progress notes are in its notebook ({notebook}) and in "
                      f"this thread. Anyone may take it; {ctx.agent_name} will reclaim it later if it is still open.")
            try:
                await ctx.release_task(t["id"], reason)
                log.info("%s: released task %s during a usage limit", ctx.agent_name, t["id"])
            except Exception as e:
                log.warning("%s: couldn't release task %s: %s", ctx.agent_name, t["id"], e)

    async def _post_report(self, ctx: AgentContext, number: int, started: datetime, result: ShiftResult) -> None:
        """One message per shift in #activity: the agent's own end-of-shift summary plus cost and outcome."""
        minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
        head = f"Shift {number} · {minutes:.0f} min · {self._cost_text(result)}"
        if result.limit:
            head += f" · usage limit: \"{result.limit}\"; next shift after {self.state.blocked_until}"
        if result.stopped:
            head += f" · stopped: {result.stopped}"
        elif result.exit_code not in (0, None):
            head += f" · exit code {result.exit_code}"
        try:
            held = await ctx.list_tasks(claimed_by=ctx.agent_name)
            if held:
                head += " · holds " + ", ".join(f"#{t['id']} {t['title']}" for t in held[:3])
            summary = result.summary.strip() or f"(no summary: the shift ended before {self.cli_name} wrote one)"
            if len(summary) > 1800:
                summary = summary[:1800] + " …[truncated]"
            await ctx.post_message(f"{head}\n{summary}", channel="activity")
        except (ToolCallError, WorkspaceUnreachable) as e:  # e.g. paused or after SWARM_END
            log.info("%s: shift report not posted: %s", ctx.agent_name, e)

    def _cost_text(self, result: ShiftResult) -> str:
        return f"${result.cost_usd:.2f} (total ${self.state.spent_usd:.2f})"

    # ------------------------------------------------------------------ shift

    def _shift_prompt(self, ctx: AgentContext, number: int, inbox: list[dict[str, Any]]) -> str:
        now = datetime.now(timezone.utc).strftime("%a %Y-%m-%d %H:%M UTC")
        lines = []
        for m in inbox[-40:]:
            where = f"task {m['task_id']}" if m.get("task_id") else f"#{m.get('channel')}"
            reply = f", reply to {m['reply_to']}" if m.get("reply_to") else ""
            body = m["body"] if len(m["body"]) <= 1500 else m["body"][:1500] + " …[truncated]"
            lines.append(f"- [id {m['id']}] {where}, {m['author_name']} ({m['author_type']}, {m['type']}{reply}): {body}")
        messages = "\n".join(lines) if lines else "(none)"
        if self.shift_budget_usd is None and self.total_budget_usd is None:
            budget = (f"No spend cap: take the shift you need for one meaningful unit of work "
                      f"(it is stopped after {self.shift_timeout_seconds / 60:.0f} min).")
        else:
            cap = f"at most ${self.shift_budget_usd:.2f}" if self.shift_budget_usd is not None else "no cap"
            total = f"${self.total_budget_usd:.2f}" if self.total_budget_usd is not None else "no limit"
            budget = f"Budget: {cap} this shift; ${self.state.spent_usd:.2f} spent so far of {total}."
        ending = (
            "Answer the new messages above as your playbook says, then end the shift."
            if self.on_mention else
            "Follow the swarm playbook in your system prompt: orient, pick one meaningful unit of work,\n"
            "do it, report it on the board, update NOTEBOOK.md, and end the shift."
        )
        return f"""\
You are swarm agent `{ctx.agent_name}`, starting shift {number}. Time now: {now}.

The board is the `{MCP_SERVER_NAME}` MCP server{self.board_tools_hint}. Pass agent_name="{ctx.agent_name}" on every call that takes it.
{budget}
Your working directory persists between shifts: {self.workdir(ctx.agent_name)}
Your notes from earlier shifts are in NOTEBOOK.md there.
The swarm repo checkout (playbook, scripts such as scripts/board.py): {self.repo_root}

New messages for you since your last shift:
{messages}

If you check read_inbox again during this shift, pass cursor={ctx.inbox_cursor}.

{ending}
"""

    def _command(self, ctx: AgentContext, prompt: str) -> list[str]:
        playbook = self.prompt_file.read_text()
        mcp_config = self._write_mcp_config(ctx)
        return [
            *self.command,
            "-p", prompt,
            "--append-system-prompt", playbook,
            "--mcp-config", str(mcp_config),
            "--strict-mcp-config",
            "--output-format", "stream-json",
            "--verbose",
            "--model", self.model,
            "--permission-mode", self.permission_mode,
            "--allowedTools", self._allowed_tools(),
            *([] if self.shift_budget_usd is None else ["--max-budget-usd", f"{self.shift_budget_usd:.2f}"]),
            *([] if not self.effort else ["--effort", self.effort]),
            "--disallowedTools", DISALLOWED_TOOLS,
            *self.extra_args,
        ]

    async def _run_shift(self, ctx: AgentContext, cmd: list[str], log_path: Path) -> ShiftResult:
        result = ShiftResult()
        env = {**os.environ, "AGENT_NAME": ctx.agent_name, "WORKSPACE_MCP_URL": ctx.client.url}
        if not self.allow_api_billing:
            for var in self.api_billing_vars:  # use the CLI's plan login, never API credits
                env.pop(var, None)
        output_done = anyio.Event()

        async with await anyio.open_process(
            cmd, cwd=self.workdir(ctx.agent_name), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        ) as proc:

            async def pump_output() -> None:
                buffer = b""
                with log_path.open("ab") as f:
                    assert proc.stdout is not None
                    async for chunk in proc.stdout:
                        f.write(chunk)
                        f.flush()
                        buffer += chunk
                        *lines, buffer = buffer.split(b"\n")
                        for line in lines:
                            self._parse_event(line, result)
                    self._parse_event(buffer, result)
                output_done.set()

            async def watch_pause() -> None:
                async with WorkspaceClient(ctx.client.url) as client:
                    while True:
                        await anyio.sleep(self.pause_poll_seconds)
                        if self._past_end():
                            log.warning("%s: SWARM_END reached; stopping the shift", ctx.agent_name)
                            result.stopped = "swarm_end"
                            await self._terminate(proc)
                            return
                        try:
                            control = await client.call("get_control_state", agent_name=ctx.agent_name)
                        except (WorkspaceUnreachable, ToolCallError):
                            continue
                        if control.get("paused"):
                            log.info("%s: paused by %s; stopping the shift", ctx.agent_name, control.get("paused_by"))
                            result.stopped = "paused"
                            await self._terminate(proc)
                            return

            async with anyio.create_task_group() as tg:
                tg.start_soon(pump_output)
                tg.start_soon(watch_pause)
                with anyio.move_on_after(self.shift_timeout_seconds) as scope:
                    await proc.wait()
                if scope.cancelled_caught:
                    log.warning("%s: shift exceeded %ss; stopping it", ctx.agent_name, self.shift_timeout_seconds)
                    result.stopped = result.stopped or "timeout"
                    await self._terminate(proc)
                with anyio.move_on_after(5):
                    await output_done.wait()
                tg.cancel_scope.cancel()
            result.exit_code = proc.returncode
        return result

    @staticmethod
    async def _terminate(proc: anyio.abc.Process) -> None:
        if proc.returncode is not None:
            return
        proc.terminate()
        with anyio.move_on_after(10):
            await proc.wait()
            return
        proc.kill()
        await proc.wait()

    @staticmethod
    def _parse_event(line: bytes, result: ShiftResult) -> None:
        line = line.strip()
        if not line.startswith(b"{"):
            # Plain lines are Claude Code's own stderr (errors such as a usage limit).
            note_limit(result, line.decode(errors="replace"))
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if event.get("type") == "result":
            # Only an errored final result carries Claude Code's error text; agent output and a
            # normal summary are ignored, so an agent quoting these messages can't trigger a backoff.
            if event.get("is_error") or event.get("subtype") not in (None, "success"):
                note_limit(result, json.dumps({k: event.get(k) for k in ("result", "errors", "error")}))
            cost = event.get("total_cost_usd")
            if isinstance(cost, (int, float)):
                result.cost_usd = float(cost)
            if isinstance(event.get("result"), str):
                result.summary = event["result"][-2000:]
