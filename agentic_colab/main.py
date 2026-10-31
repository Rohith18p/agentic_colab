"""The FastAPI app: dashboard, JSON API for humans, live event stream, and MCP at /mcp."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import anyio
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from .config import Settings
from .mcp_tools import build_mcp_server
from .workspace import CHARTER_WARNING, Workspace, WorkspaceError

log = logging.getLogger("agentic_colab")
HERE = Path(__file__).parent
NAME_COOKIE = "display_name"


class MessageIn(BaseModel):
    body: str
    type: str = "human_input"
    channel: str | None = None
    task_id: int | None = None
    reply_to: int | None = None
    mentions: list[str] | None = None


class TaskIn(BaseModel):
    title: str
    description: str = ""
    parent_id: int | None = None


class TaskUpdateIn(BaseModel):
    status: str | None = None
    note: str | None = None


class CharterIn(BaseModel):
    objective: str = ""
    constraints: str = ""
    libraries: str = ""
    compute: str = ""
    notes: str = ""
    base_version: int | None = None


class PauseIn(BaseModel):
    reason: str = ""


def human_name(request: Request) -> str:
    """The human's display name, from the cookie the dashboard sets on first visit."""
    name = unquote(request.cookies.get(NAME_COOKIE, "") or request.headers.get("x-display-name", "")).strip()
    if not name:
        raise HTTPException(400, detail={"error": "no_name", "message": "Set a display name first."})
    return name[:100]


async def run(fn, *args, **kwargs) -> Any:
    """Run a sync workspace call off the event loop, mapping errors to HTTP."""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except WorkspaceError as e:
        raise HTTPException(e.status, detail=e.to_dict()) from e


def build_api(ws: Workspace, boot_id: str) -> APIRouter:
    api = APIRouter(prefix="/api")

    @api.get("/state")
    async def state():
        return {**await run(ws.snapshot), "boot_id": boot_id}

    @api.get("/messages")
    async def messages(channel: str | None = None, task_id: int | None = None, after_id: int | None = None, limit: int = 100):
        return await run(ws.list_messages, channel=channel, task_id=task_id, after_id=after_id, limit=limit)

    @api.post("/messages")
    async def post_message(m: MessageIn, request: Request):
        name = human_name(request)
        return await run(
            ws.post_message, name, "human", m.type, m.body, channel=m.channel, task_id=m.task_id,
            reply_to=m.reply_to, mentions=m.mentions,
        )

    @api.post("/questions/{message_id}/resolve")
    async def resolve(message_id: int, request: Request):
        return await run(ws.resolve_question, message_id, human_name(request))

    @api.get("/charter")
    async def charter():
        return await run(ws.read_charter)

    @api.get("/charter/history")
    async def charter_history():
        return await run(ws.charter_history)

    @api.put("/charter")
    async def update_charter(c: CharterIn, request: Request):
        fields = c.model_dump(exclude={"base_version"})
        return await run(ws.update_charter, human_name(request), fields, base_version=c.base_version)

    @api.get("/tasks")
    async def tasks(status: str | None = None):
        return await run(ws.list_tasks, status=status)

    @api.post("/tasks")
    async def create_task(t: TaskIn, request: Request):
        return await run(ws.create_task, human_name(request), "human", t.title, description=t.description, parent_id=t.parent_id)

    @api.get("/tasks/{task_id}")
    async def get_task(task_id: int):
        return await run(ws.get_task, task_id)

    @api.patch("/tasks/{task_id}")
    async def update_task(task_id: int, u: TaskUpdateIn, request: Request):
        return await run(ws.update_task, human_name(request), "human", task_id, status=u.status, note=u.note)

    @api.post("/tasks/{task_id}/release")
    async def release_task(task_id: int, request: Request):
        return await run(ws.release_task, human_name(request), "human", task_id)

    @api.get("/agents")
    async def agents():
        return await run(ws.list_agents)

    @api.post("/agents/{name}/pause")
    async def pause(name: str, request: Request, p: PauseIn | None = None):
        return await run(ws.pause_agent, name, human_name(request), reason=p.reason if p else "")

    @api.post("/agents/{name}/resume")
    async def resume(name: str, request: Request):
        return await run(ws.resume_agent, name, human_name(request))

    @api.get("/events")
    async def events(after_id: int = 0, limit: int = 200):
        return await run(ws.list_events, after_id=after_id, limit=limit)

    @api.get("/stream")
    async def stream(request: Request, after_id: int | None = None):
        """Server-sent events: one `event` per row appended to the events log."""
        cursor = after_id if after_id is not None else await run(ws.last_event_id)

        async def gen():
            nonlocal cursor
            entry = ws.notifier.subscribe()
            _, wake = entry
            try:
                yield "retry: 2000\n\n"
                # Lets dashboards notice a restarted server (e.g. after a reload) and refresh themselves.
                yield f"event: hello\ndata: {json.dumps({'boot_id': boot_id})}\n\n"
                while not ws.notifier.closed and not await request.is_disconnected():
                    wake.clear()
                    rows = await run(ws.list_events, after_id=cursor, limit=200)
                    for row in rows:
                        cursor = row["id"]
                        yield f"id: {row['id']}\nevent: workspace\ndata: {json.dumps(row)}\n\n"
                    if rows or ws.notifier.closed:
                        continue
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                ws.notifier.unsubscribe(entry)

        return StreamingResponse(
            gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    return api


async def lease_sweeper(ws: Workspace) -> None:
    while True:
        await asyncio.sleep(ws.settings.lease_sweep_seconds)
        try:
            reopened = await anyio.to_thread.run_sync(ws.reap_expired_leases)
            if reopened:
                log.info("lease expired; reopened tasks %s", reopened)
        except Exception:  # keep sweeping even if one pass fails
            log.exception("lease sweep failed")


def create_app(settings: Settings | None = None) -> FastAPI:
    ws = Workspace(settings)
    mcp = build_mcp_server(ws)
    if ws.settings.mcp_allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=ws.settings.mcp_allowed_hosts,
            allowed_origins=[f"http://{h}" for h in ws.settings.mcp_allowed_hosts]
            + [f"https://{h}" for h in ws.settings.mcp_allowed_hosts],
        )
    else:
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    # Stateless + JSON responses: every tool call is a plain request/response, so
    # agents survive server restarts without session bookkeeping.
    mcp_app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        sweeper = asyncio.create_task(lease_sweeper(ws))
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

    app = FastAPI(title="agentic_colab", lifespan=lifespan)
    app.state.workspace = ws
    app.state.mcp = mcp
    app.state.boot_id = uuid.uuid4().hex
    templates = Jinja2Templates(directory=HERE / "templates")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(request, "index.html", {"charter_warning": CHARTER_WARNING})

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": "http_error", "message": str(exc.detail)}
        return JSONResponse(detail, status_code=exc.status_code)

    app.include_router(build_api(ws, app.state.boot_id))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    # Last: the MCP app's only route is /mcp; everything above wins first.
    app.mount("/", mcp_app)
    return app

