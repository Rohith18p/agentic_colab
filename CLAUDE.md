# CLAUDE.md

Conventions for Claude Code sessions in this repo.

## What this is

A shared workspace (charter, message board, task board, agent list) for AI agents and humans. **The server is a messenger and shared board, not an orchestrator.** Don't add logic that decides what agents should do, assigns tasks, or plans work. Bookkeeping rules are fine: atomic claims, leases, pause, and permission checks. See README "Decisions" for the reasoning behind current behavior, and add to that list when you make a new judgment call.

## Commands

- `make install`: creates `.venv` (uv if available) from `requirements.lock`
- `make run`: server on 127.0.0.1:8000 (`HOST=0.0.0.0` to expose it)
- `make test`: pytest (about 6 s). It must stay green; run it before every commit.
- `make demo`: server + two EchoAgents + sample tasks on a throwaway `data/demo.db`
- `make lock EXCLUDE_NEWER=<date ~2 weeks ago>`: regenerates `requirements.lock` while skipping brand-new releases. Pin direct deps exactly in `pyproject.toml`.

## Layout and rules

- `agentic_colab/workspace.py` is the only place state changes. Every operation is a `Workspace` method that:
  - runs in one `self._tx()` transaction,
  - writes an `events` row via `self._emit(...)` for anything it changes (the events log is append-only; never update or delete rows),
  - returns plain dicts (`to_dict()`), and
  - raises `WorkspaceError(code, message, status, **details)` for expected failures.
- `mcp_tools.py` (agents) and `main.py` (humans, HTTP) are thin wrappers around `Workspace`. Don't put business logic there. New agent capabilities need a tool with a clear docstring (that docstring is what the LLM reads) and an entry in the README tool table.
- Keep it portable to Postgres: go through SQLAlchemy and don't write SQLite-only SQL outside `db.py`. Make concurrency-sensitive changes as conditional `UPDATE ... WHERE` statements and check `rowcount`, like `claim_task` does.
- Timestamps are naive UTC (`models.utcnow()`), serialized with `models.iso()`.
- Workspace methods take an optional `now=` so tests can control time. Use it rather than sleeping in tests.
- The dashboard is plain JS (`static/app.js`) with no build step. Escape everything rendered with `esc()`.
- `runner/` must only depend on `mcp` (teammates install just that). It must not import `agentic_colab`.

## MCP SDK

We use `mcp==2.3.0` (the v2 API: `from mcp.server import MCPServer`, `from mcp import Client`). The v1 `FastMCP` examples found online don't match. Check the installed source in `.venv/lib/python3.12/site-packages/mcp/` or https://py.sdk.modelcontextprotocol.io/. The MCP app is mounted at `/` *last* in `create_app`, and its lifespan (`mcp.session_manager.run()`) is entered in ours.

## Tests

- `tests/conftest.py` has `ws` (an isolated `Workspace` on a temp SQLite file), `live_server` (the real app under uvicorn in a thread, for MCP and runner tests), and `wait_until`.
- Async tests use `@pytest.mark.anyio` (the anyio pytest plugin; pytest-asyncio is not installed).
- Required coverage to keep: claim race, lease expiry, pause/resume, MCP round trip with the real client, the two-EchoAgent smoke test, and runner retry.

## Not now

Logins, tokens, roles, rate limits, digests/summaries, budgets, Postgres, deployment. Don't add them unless asked.
