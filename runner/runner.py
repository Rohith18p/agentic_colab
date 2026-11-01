"""The runner loop: register, heartbeat, honour pause, call the agent's step()."""

from __future__ import annotations

import logging
import re
import socket

import anyio

from .agent import Agent, AgentContext
from .client import ToolCallError, WorkspaceClient, WorkspaceUnreachable

log = logging.getLogger("runner")


def _host(machine: str) -> str:
    """The hostname part of a board machine label such as "box · 1x RTX 4090" or "mac (Apple M4)"."""
    return machine.split(" · ")[0].split(" (")[0].strip()


def _slug(text: str) -> str:
    """Letters, digits, '_', '.', '-' only (board name rules), at most 20 characters."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text.split(".")[0]).strip("-")[:20].strip("-")


class Runner:
    def __init__(
        self,
        workspace_url: str,
        agent_name: str,
        agent: Agent,
        *,
        owner_name: str = "",
        machine: str | None = None,
        step_interval: float = 1.0,
        idle_interval: float = 3.0,
        retry_interval: float = 3.0,
        heartbeat_interval: float | None = None,
    ) -> None:
        self.workspace_url = workspace_url
        self.agent_name = agent_name
        self.agent = agent
        self.owner_name = owner_name
        self.machine = machine if machine is not None else socket.gethostname()
        self.step_interval = step_interval
        self.idle_interval = idle_interval
        self.retry_interval = retry_interval
        self.heartbeat_interval = heartbeat_interval
        # Observable state (handy for tests and logs).
        self.registered = False
        self._name_checked = False
        self.paused = False
        self.steps = 0
        self._stop = anyio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        with anyio.move_on_after(seconds):
            await self._stop.wait()

    async def run(self) -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._heartbeat_loop)
            try:
                await self._main_loop()
            finally:
                tg.cancel_scope.cancel()

    async def _unique_name(self, client: WorkspaceClient) -> str:
        """The launch name, or an alias if another person's agent on another machine already uses it.

        The board merges agents that register under one name (one inbox, one set of claims), so
        two people who both start "scout-1" would share an identity. Same owner or same host
        means it's this agent restarting, and the name is kept.
        """
        try:
            agents = (await client.call("list_agents")).get("agents") or []
        except ToolCallError as e:
            log.warning("couldn't list agents to check the name %s: %s", self.agent_name, e)
            return self.agent_name
        taken = {a["name"]: a for a in agents}
        me_host, me_owner = _host(self.machine), self.owner_name

        def clashes(name: str) -> bool:
            other = taken.get(name)
            if other is None:
                return False
            host, owner = _host(other.get("machine") or ""), other.get("owner_name") or ""
            if not host or host == name or host == me_host:  # unknown or this machine
                return False
            return not (owner and me_owner and owner == me_owner)

        if not clashes(self.agent_name):
            return self.agent_name
        suffixes = [s for s in (_slug(me_owner), _slug(me_host)) if s]
        candidates = [f"{self.agent_name}-{s}" for s in suffixes]
        candidates += [f"{candidates[-1] if candidates else self.agent_name}-{i}" for i in range(2, 10)]
        for name in candidates:
            name = name[:64]
            if not clashes(name):
                other = taken[self.agent_name]
                log.warning("the name %s is already used by %s's agent on %s; registering as %s",
                            self.agent_name, other.get("owner_name") or "someone",
                            _host(other.get("machine") or ""), name)
                return name
        return self.agent_name

    async def _register(self, client: WorkspaceClient, ctx: AgentContext) -> None:
        if not self._name_checked:
            self._name_checked = True
            self.agent_name = ctx.agent_name = await self._unique_name(client)
        info = await client.call(
            "register_agent",
            agent_name=self.agent_name,
            owner_name=self.owner_name,
            machine=self.machine,
            skills=list(self.agent.skills),
        )
        if self.heartbeat_interval is None:
            self.heartbeat_interval = float(info.get("heartbeat_interval_seconds", 10))
        await self.agent.setup(ctx)
        self.registered = True
        log.info("%s registered with %s", self.agent_name, self.workspace_url)

    async def _main_loop(self) -> None:
        async with WorkspaceClient(self.workspace_url) as client:
            ctx = AgentContext(client, self.agent_name)
            while not self._stop.is_set():
                if getattr(self.agent, "done", False):  # the agent says it is finished for good
                    log.info("%s: agent finished; stopping the runner", self.agent_name)
                    self._stop.set()
                    break
                try:
                    if not self.registered:
                        await self._register(client, ctx)
                    control = await client.call("get_control_state", agent_name=self.agent_name)
                    if control["paused"]:
                        if not self.paused:
                            log.info("%s paused by %s", self.agent_name, control.get("paused_by"))
                        self.paused = True
                        await self._sleep(self.step_interval)
                        continue
                    if self.paused:
                        log.info("%s resumed", self.agent_name)
                        self.paused = False
                    did_work = await self.agent.step(ctx)
                    self.steps += 1
                    await self._sleep(self.step_interval if did_work else self.idle_interval)
                except WorkspaceUnreachable as e:
                    log.warning("workspace unreachable (%s); retrying in %ss", e, self.retry_interval)
                    await self._sleep(self.retry_interval)
                except ToolCallError as e:
                    if e.code == "not_registered":
                        self.registered = False
                    elif e.code == "agent_paused":
                        self.paused = True  # paused mid-step; the next loop waits
                    else:
                        log.warning("%s: tool call rejected: %s", self.agent_name, e)
                        await self._sleep(self.idle_interval)

    async def _heartbeat_loop(self) -> None:
        async with WorkspaceClient(self.workspace_url) as client:
            while not self._stop.is_set():
                if not self.registered:
                    await self._sleep(0.5)
                    continue
                try:
                    await client.call("heartbeat", agent_name=self.agent_name)
                except WorkspaceUnreachable as e:
                    log.debug("heartbeat failed: %s", e)
                    await self._sleep(self.retry_interval)
                    continue
                except ToolCallError as e:
                    if e.code == "not_registered":
                        self.registered = False
                    else:
                        log.warning("heartbeat rejected: %s", e)
                await self._sleep(self.heartbeat_interval or 10)
