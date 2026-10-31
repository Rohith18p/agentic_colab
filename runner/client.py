"""A thin MCP client for the workspace that reconnects after failures."""

from __future__ import annotations

import contextlib
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client

log = logging.getLogger("runner.client")


class WorkspaceUnreachable(Exception):
    """The server could not be reached (or the connection broke). Retry later."""


class ToolCallError(Exception):
    """The server rejected the call, e.g. `not_claimable` or `agent_paused`."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data or {}


def mcp_url(workspace_url: str) -> str:
    url = workspace_url.rstrip("/")
    return url if url.endswith("/mcp") else url + "/mcp"


def _parse_error(text: str) -> ToolCallError:
    # The server sends a JSON object; the SDK may prefix it with "Error executing tool ...: ".
    start = text.find("{")
    if start != -1:
        with contextlib.suppress(ValueError):
            data = json.loads(text[start:])
            if isinstance(data, dict) and "error" in data:
                return ToolCallError(data["error"], data.get("message", ""), data)
    return ToolCallError("tool_error", text)


class WorkspaceClient:
    """Call workspace MCP tools; connects lazily and reconnects after errors.

    One instance should be used from a single asyncio task.
    """

    def __init__(self, workspace_url: str, read_timeout_seconds: float = 30) -> None:
        self.url = mcp_url(workspace_url)
        self.read_timeout_seconds = read_timeout_seconds
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    async def __aenter__(self) -> WorkspaceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def _connect(self) -> Client:
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                Client(self.url, read_timeout_seconds=self.read_timeout_seconds)
            )
        except Exception as e:
            with contextlib.suppress(Exception):
                await stack.aclose()
            raise WorkspaceUnreachable(f"cannot connect to {self.url}: {e!r}") from e
        self._stack, self._client = stack, client
        return client

    async def close(self) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        client = self._client or await self._connect()
        args = {k: v for k, v in arguments.items() if v is not None}
        try:
            result = await client.call_tool(tool, args)
        except Exception as e:
            await self.close()
            raise WorkspaceUnreachable(f"{tool} failed: {e!r}") from e
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            raise _parse_error(text)
        return result.structured_content or {}
