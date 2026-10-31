"""The runner loop: register, heartbeat, honour pause, call the agent's step()."""

from __future__ import annotations

import logging
import socket

import anyio

from .agent import Agent, AgentContext
from .client import ToolCallError, WorkspaceClient, WorkspaceUnreachable

log = logging.getLogger("runner")


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

    async def _register(self, client: WorkspaceClient, ctx: AgentContext) -> None:
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
