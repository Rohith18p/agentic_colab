from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
import uvicorn

from agentic_colab.config import Settings
from agentic_colab.main import create_app
from agentic_colab.workspace import Workspace


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        lease_seconds=30,
        lease_sweep_seconds=0.2,
        offline_after_seconds=60,
        mcp_allowed_hosts=[],
    )


@pytest.fixture
def ws(settings) -> Workspace:
    return Workspace(settings)


@dataclass
class LiveServer:
    url: str
    ws: Workspace


def free_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    return sock


@contextmanager
def serve(app, sock: socket.socket) -> Iterator[None]:
    """Serve `app` with uvicorn on an already-bound socket, in a background thread."""
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def live_server(settings) -> Iterator[LiveServer]:
    """The real app (dashboard API + MCP) served by uvicorn on a free port."""
    app = create_app(settings)
    sock = free_socket()
    port = sock.getsockname()[1]
    try:
        with serve(app, sock):
            yield LiveServer(f"http://127.0.0.1:{port}", app.state.workspace)
    finally:
        sock.close()


async def wait_until(predicate, timeout: float = 15, interval: float = 0.05, message: str = "condition"):
    """Poll `predicate` (sync or async) until truthy, or fail the test."""
    import anyio

    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if hasattr(result, "__await__"):
            result = await result
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {message}")
        await anyio.sleep(interval)
