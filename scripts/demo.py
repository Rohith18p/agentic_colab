"""`make demo`: a fresh server, two EchoAgent runners, and a few sample tasks.

Open http://localhost:8000 and watch the agents work. Ctrl-C stops everything.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("PORT", "8000"))
URL = f"http://127.0.0.1:{PORT}"
DB = ROOT / "data" / "demo.db"

CHARTER = {
    "objective": "Demo project: show agents and humans coordinating on a shared board.",
    "constraints": "EchoAgents use no LLM and cost nothing. Keep steps small so Pause takes effect quickly.",
    "libraries": "Python 3.12, FastAPI, MCP Python SDK",
    "compute": "Whatever laptop this runs on.",
    "notes": "Try: pause an agent, @mention one in #general, answer a question in the Needs human panel.",
}

TASKS = [
    ("Collect requirements", "Summarize what the team wants from the workspace."),
    ("Draft the data model", "Tables for events, charter, messages, tasks and agents."),
    ("Pick a license", "[ask] EchoAgent will ask a human before going ahead."),
    ("Write the README", "How to run, how to connect a runner."),
    ("Smoke-test the runner", "Make sure a runner survives a server restart."),
]


def request(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Display-Name": "demo-script"},
    )
    with urllib.request.urlopen(req, timeout=5) as res:
        return json.loads(res.read())


def wait_for_server(proc: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit("server exited during startup (is port %d in use?)" % PORT)
        try:
            request("GET", "/healthz")
            return
        except OSError:
            time.sleep(0.3)
    sys.exit("server did not start")


def _interrupt(_signum, _frame) -> None:
    raise KeyboardInterrupt


def main() -> None:
    # Handle Ctrl-C and `kill` the same way, even when started in the background.
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)
    for suffix in ("", "-wal", "-shm"):
        Path(str(DB) + suffix).unlink(missing_ok=True)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{DB}", "LEASE_SECONDS": "30"}
    procs: list[subprocess.Popen] = []
    server = subprocess.Popen(
        [sys.executable, "-m", "agentic_colab", "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    procs.append(server)
    try:
        wait_for_server(server)
        request("PUT", "/api/charter", CHARTER)
        for title, description in TASKS:
            request("POST", "/api/tasks", {"title": title, "description": description})
        request("POST", "/api/messages", {"body": "Welcome! Sample tasks are on the board.", "channel": "general"})
        for name, owner in (("echo-alpha", "demo"), ("echo-bravo", "demo")):
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "runner"], cwd=ROOT,
                env={**env, "WORKSPACE_URL": URL, "AGENT_NAME": name, "AGENT_OWNER": owner, "STEP_SECONDS": "2"},
            ))
        print(f"\n  Demo running: open {URL}\n  Ctrl-C to stop.\n", flush=True)
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        print("a demo process exited; shutting down")
    except KeyboardInterrupt:
        pass
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for p in reversed(procs):
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
