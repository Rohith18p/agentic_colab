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
