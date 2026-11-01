"""The runner waits and retries while the server is unreachable."""

from __future__ import annotations

import anyio
import pytest

from agentic_colab.main import create_app
from runner import EchoAgent, Runner

from .conftest import free_socket, serve, wait_until


@pytest.mark.anyio
async def test_runner_retries_until_server_is_up(settings):
    sock = free_socket()  # bound but not listening: connections are refused
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    runner = Runner(url, "patient", EchoAgent(work_steps=1), step_interval=0.05, idle_interval=0.1,
                    retry_interval=0.2, heartbeat_interval=0.3)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.run)
            await anyio.sleep(1)
            assert not runner.registered

            app = create_app(settings)
            ws = app.state.workspace
            with serve(app, sock):
                await wait_until(lambda: runner.registered, message="runner registers once the server is up")
                task = ws.create_task("alice", "human", "after outage")
                await wait_until(lambda: ws.get_task(task["id"])["status"] == "completed", message="task completed")
                runner.stop()
    finally:
        sock.close()


@pytest.mark.anyio
async def test_a_name_taken_on_another_machine_gets_an_alias(live_server):
    ws = live_server.ws
    ws.register_agent("scout-1", owner_name="sha", machine="sha-devel · 1x RTX 4070")
    ws.register_agent("bob-1", owner_name="bob", machine="box (Apple M4)")
    ws.register_agent("odd-1", owner_name="nail", machine="odd-1")  # an agent that re-registered oddly

    async def start(name, owner, machine):
        runner = Runner(live_server.url, name, EchoAgent(work_steps=0), owner_name=owner, machine=machine,
                        step_interval=0.05, idle_interval=0.1, heartbeat_interval=0.3)
        async with anyio.create_task_group() as tg:
            tg.start_soon(runner.run)
            await wait_until(lambda: runner.registered, message=f"{name} registers")
            runner.stop()
        return runner.agent_name

    laptop = "nail-Legion-Pro-7 · 1x RTX 5090"
    assert await start("scout-1", "nail", laptop) == "scout-1-nail"   # someone else's name: alias
    assert await start("scout-1", "nail", laptop) == "scout-1-nail"   # restart: same alias again
    assert await start("scout-1", "sha", "sha-devel · 1x RTX 4070") == "scout-1"  # the original keeps it
    assert await start("bob-1", "bob", "box · Apple M4 · 16 GB") == "bob-1"  # same host, new label
    assert await start("odd-1", "nail", laptop) == "odd-1"  # same owner
    assert await start("fresh-1", "nail", laptop) == "fresh-1"
    assert {a["name"]: a["owner_name"] for a in ws.list_agents()}["scout-1"] == "sha"
