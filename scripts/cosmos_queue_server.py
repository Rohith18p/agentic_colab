#!/usr/bin/env python3
"""A shared, durable job queue in front of every Cosmos endpoint in the swarm.

Agents submit clip jobs with scripts/cosmos_queue.py and leave; one worker per endpoint feeds
each job to whichever endpoint is free, and the clip plus its JSON sidecar stay on this machine
until someone fetches them. Jobs survive client crashes, shift ends and server restarts.

    python3 scripts/cosmos_queue_server.py --host <tailscale-ip> --port 8030 \
        --endpoint gb10=http://127.0.0.1:8011 --endpoint a100=http://100.111.167.68:8014 ...

Standard library only. State lives in --root (default ~/jobs/queue): one folder per job with
job.json, the input image, and when done out.mp4 + out.json. Bind to the tailnet address only.

API (JSON):
  POST /jobs            {"owner", "name", "prompt", optional width/height/num_frames/fps/steps/guidance/
                         flow_shift/seed/negative_prompt/extra_params, "image_b64" + "image_name",
                         "endpoints": [names]}  -> {"id", "status"}
  GET  /jobs?owner=X    list jobs (all owners if omitted)
  GET  /jobs/<id>       one job, with its sidecar once done
  GET  /jobs/<id>/mp4   the clip
  DELETE /jobs/<id>     cancel a pending job
  GET  /status          endpoints, queue length per owner
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULTS = dict(width=832, height=480, num_frames=189, fps=24, steps=35, guidance=6.0, flow_shift=10.0, seed=1,
                negative_prompt=None, extra_params={})
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
MAX_PENDING_PER_OWNER = 60
MAX_BODY = 25 * 1024 * 1024
LOCK = threading.Lock()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Queue:
    def __init__(self, root: Path, endpoints: dict[str, str]):
        self.root = root
        self.endpoints = endpoints
        self.endpoint_state = {n: {"url": u, "job": None, "ok": None, "done": 0, "failed": 0} for n, u in endpoints.items()}
        root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, dict] = {}
        for d in sorted(root.iterdir()):
            f = d / "job.json"
            if f.exists():
                job = json.loads(f.read_text())
                if job["status"] == "running":  # the server stopped mid-job: run it again
                    job.update(status="pending", endpoint=None)
                    self._save(job)
                self.jobs[job["id"]] = job

    def _save(self, job: dict) -> None:
        d = self.root / job["id"]
        d.mkdir(exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(job, indent=1))
        tmp.replace(d / "job.json")

    def submit(self, body: dict) -> dict:
        owner, name = str(body.get("owner", "")), str(body.get("name", ""))
        if not NAME_RE.match(owner) or not NAME_RE.match(name):
            raise ValueError("owner and name must be 1-96 characters: letters, digits, '_', '.', '-'")
        if not str(body.get("prompt", "")).strip():
            raise ValueError("prompt is required")
        unknown = [e for e in body.get("endpoints") or [] if e not in self.endpoints]
        if unknown:
            raise ValueError(f"unknown endpoints {unknown}; known: {sorted(self.endpoints)}")
        with LOCK:
            pending = sum(1 for j in self.jobs.values() if j["owner"] == owner and j["status"] == "pending")
            if pending >= MAX_PENDING_PER_OWNER:
                raise ValueError(f"{owner} already has {pending} pending jobs (limit {MAX_PENDING_PER_OWNER})")
            jid = time.strftime("%H%M%S", time.gmtime()) + "-" + secrets.token_hex(3)
            params = {k: body.get(k, v) for k, v in DEFAULTS.items()}
            seq = 1 + max((j.get("seq", 0) for j in self.jobs.values()), default=0)
            job = dict(id=jid, seq=seq, owner=owner, name=name, prompt=body["prompt"], params=params,
                       endpoints=body.get("endpoints") or None, status="pending", submitted=now(),
                       started=None, finished=None, endpoint=None, attempts=0, error=None, image=None)
            d = self.root / jid
            d.mkdir()
            if body.get("image_b64"):
                image_name = os.path.basename(str(body.get("image_name") or "input.png")) or "input.png"
                (d / image_name).write_bytes(base64.b64decode(body["image_b64"]))
                job["image"] = image_name
            self._save(job)
            self.jobs[jid] = job
        return job

    def cancel(self, jid: str) -> dict:
        with LOCK:
            job = self.jobs[jid]
            if job["status"] != "pending":
                raise ValueError(f"job is {job['status']}; only pending jobs can be cancelled")
            job["status"] = "cancelled"
            job["finished"] = now()
            self._save(job)
        return job

    def next_job(self, endpoint: str) -> dict | None:
        """Oldest pending job of the owner with the fewest jobs running, so no one owner hogs the GPUs."""
        with LOCK:
            pending = [j for j in self.jobs.values() if j["status"] == "pending"
                       and (not j["endpoints"] or endpoint in j["endpoints"])]
            if not pending:
                return None
            running = {}
            for j in self.jobs.values():
                if j["status"] == "running":
                    running[j["owner"]] = running.get(j["owner"], 0) + 1
            job = min(pending, key=lambda j: (running.get(j["owner"], 0), j.get("seq", 0)))
            job.update(status="running", endpoint=endpoint, started=now(), attempts=job["attempts"] + 1)
            self._save(job)
            return job

    def finish(self, job: dict, ok: bool, error: str | None = None, retry: bool = False) -> None:
        with LOCK:
            if retry and job["attempts"] < 3:
                job.update(status="pending", endpoint=None, error=error)
            else:
                job.update(status="done" if ok else "failed", finished=now(), error=error)
            self._save(job)

    def view(self, job: dict) -> dict:
        out = {k: v for k, v in job.items()}
        side = self.root / job["id"] / "out.json"
        if job["status"] == "done" and side.exists():
            out["sidecar"] = json.loads(side.read_text())
        return out


def health(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def multipart(fields: dict[str, str], file: tuple[str, bytes] | None) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    if file:
        name, data = file
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="input_reference"; filename="{name}"\r\n'
                     f'Content-Type: image/png\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def run_job(q: Queue, name: str, url: str, job: dict) -> None:
    p = job["params"]
    fields = dict(prompt=job["prompt"], size=f"{p['width']}x{p['height']}", num_frames=str(p["num_frames"]),
                  fps=str(p["fps"]), num_inference_steps=str(p["steps"]), guidance_scale=str(p["guidance"]),
                  flow_shift=str(p["flow_shift"]), seed=str(p["seed"]),
                  extra_params=json.dumps({"client": job["owner"], "queue_id": job["id"], **(p["extra_params"] or {})}))
    if p["negative_prompt"]:
        fields["negative_prompt"] = p["negative_prompt"]
    d = q.root / job["id"]
    file = (job["image"], (d / job["image"]).read_bytes()) if job["image"] else None
    body, ctype = multipart(fields, file)
    t = time.time()
    req = urllib.request.Request(f"{url.rstrip('/')}/v1/videos/sync", data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=7200) as r:
            mp4 = r.read()
            headers = {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:  # the server rejected this job: don't retry it elsewhere blindly
        detail = e.read()[:500].decode(errors="replace")
        q.finish(job, False, f"HTTP {e.code} from {name}: {detail}", retry=e.code >= 500)
        return
    except Exception as e:  # endpoint down or restarting: retry on any endpoint
        q.finish(job, False, f"{name}: {e}", retry=True)
        return
    (d / "out.mp4.tmp").write_bytes(mp4)
    (d / "out.mp4.tmp").replace(d / "out.mp4")
    sidecar = {"name": job["name"], **p, "prompt_text": job["prompt"], "negative_prompt_text": p["negative_prompt"],
               "image": job["image"], "owner": job["owner"], "queue_id": job["id"], "endpoint": name,
               "endpoint_url": url, "server_clip_id": headers.get("x-clip-id"),
               "seconds": float(headers.get("x-seconds", 0) or 0), "wall_seconds": round(time.time() - t, 1),
               "model": "nvidia/Cosmos3-Nano via the endpoint above; check that endpoint's #compute post for its stack"}
    (d / "out.json").write_text(json.dumps(sidecar, indent=1))
    q.finish(job, True)


def worker(q: Queue, name: str, url: str, stop: threading.Event, poll: float) -> None:
    state = q.endpoint_state[name]
    while not stop.is_set():
        h = health(url)
        state["ok"] = h is not None
        state["health"] = h
        if not h or h.get("busy") or h.get("waiting"):  # down, or busy with someone's direct request
            stop.wait(poll)
            continue
        job = q.next_job(name)
        if job is None:
            stop.wait(poll)
            continue
        state["job"] = job["id"]
        run_job(q, name, url, job)
        state["job"] = None
        if job["status"] in ("done", "failed"):
            state[job["status"]] += 1


def make_handler(q: Queue):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def send(self, code: int, obj) -> None:
            data = json.dumps(obj, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def job_from_path(self, path: str) -> dict | None:
            m = re.match(r"^/jobs/([0-9a-f-]+)(/mp4)?$", path)
            return q.jobs.get(m.group(1)) if m else None

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/status":
                counts: dict[str, dict] = {}
                for j in q.jobs.values():
                    counts.setdefault(j["owner"], {}).setdefault(j["status"], 0)
                    counts[j["owner"]][j["status"]] += 1
                eps = {n: {k: v for k, v in s.items()} for n, s in q.endpoint_state.items()}
                return self.send(200, {"endpoints": eps, "owners": counts, "time": now()})
            if u.path == "/jobs":
                owner = parse_qs(u.query).get("owner", [None])[0]
                jobs = [q.view(j) for j in sorted(q.jobs.values(), key=lambda j: j.get("seq", 0))
                        if owner is None or j["owner"] == owner]
                return self.send(200, {"jobs": jobs})
            job = self.job_from_path(u.path)
            if job is None:
                return self.send(404, {"error": "no such job"})
            if u.path.endswith("/mp4"):
                f = q.root / job["id"] / "out.mp4"
                if not f.exists():
                    return self.send(409, {"error": f"job is {job['status']}"})
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            return self.send(200, q.view(job))

        def do_POST(self):
            if urlparse(self.path).path != "/jobs":
                return self.send(404, {"error": "POST /jobs only"})
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY:
                return self.send(413, {"error": "body too large"})
            try:
                job = q.submit(json.loads(self.rfile.read(n)))
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return self.send(400, {"error": str(e)})
            return self.send(201, {"id": job["id"], "status": job["status"]})

        def do_DELETE(self):
            job = self.job_from_path(urlparse(self.path).path)
            if job is None:
                return self.send(404, {"error": "no such job"})
            try:
                return self.send(200, q.view(q.cancel(job["id"])))
            except ValueError as e:
                return self.send(409, {"error": str(e)})

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="bind address: this machine's Tailscale IP")
    ap.add_argument("--port", type=int, default=8030)
    ap.add_argument("--root", default=os.path.expanduser("~/jobs/queue"))
    ap.add_argument("--endpoint", action="append", default=[], metavar="NAME=URL", required=True)
    ap.add_argument("--poll", type=float, default=5.0)
    a = ap.parse_args()
    endpoints = dict(e.split("=", 1) for e in a.endpoint)
    q = Queue(Path(a.root), endpoints)
    stop = threading.Event()
    for name, url in endpoints.items():
        threading.Thread(target=worker, args=(q, name, url, stop, a.poll), daemon=True).start()
    server = ThreadingHTTPServer((a.host, a.port), make_handler(q))
    print(f"cosmos queue on http://{a.host}:{a.port}, {len(q.jobs)} jobs on disk, endpoints {endpoints}", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
