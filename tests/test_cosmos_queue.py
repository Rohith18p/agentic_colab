"""The shared Cosmos job queue, against fake endpoints (no GPU)."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cosmos_queue_server as qs  # noqa: E402


def fake_endpoint(seen: list, fail_first: int = 0):
    """A /health + /v1/videos/sync server that records each request and returns fake mp4 bytes."""
    state = {"fails": fail_first}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = json.dumps({"busy": False, "waiting": 0}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            if state["fails"]:
                state["fails"] -= 1
                self.send_response(503)
                self.end_headers()
                return
            seen.append(data)
            time.sleep(0.2)
            self.send_response(200)
            self.send_header("X-Clip-Id", f"clip{len(seen)}")
            self.send_header("X-Seconds", "1.5")
            self.end_headers()
            self.wfile.write(b"MP4" + data[-20:])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def start_queue(root: Path, endpoints: dict[str, str]):
    q = qs.Queue(root, endpoints)
    stop = threading.Event()
    for n, u in endpoints.items():
        threading.Thread(target=qs.worker, args=(q, n, u, stop, 0.05), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), qs.make_handler(q))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return q, stop, srv, f"http://127.0.0.1:{srv.server_address[1]}"


def cli(url: str, *args: str, cwd: Path) -> str:
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "cosmos_queue.py"), "--url", url, *args],
                       capture_output=True, text=True, cwd=cwd, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def wait(pred, timeout=15):
    t = time.time()
    while not pred():
        assert time.time() - t < timeout, "timed out"
        time.sleep(0.05)


def test_jobs_run_on_free_endpoints_and_survive_the_client(tmp_path):
    seen_a, seen_b = [], []
    ea, ua = fake_endpoint(seen_a)
    eb, ub = fake_endpoint(seen_b, fail_first=1)  # one 503: the job is retried, not lost
    q, stop, srv, url = start_queue(tmp_path / "queue", {"a": ua, "b": ub})
    (tmp_path / "frame.png").write_bytes(b"PNGDATA")
    jobs = tmp_path / "jobs.jsonl"
    jobs.write_text("\n".join(json.dumps(j) for j in [
        {"name": "ball_s1", "prompt": "a ball falls", "image": "frame.png", "seed": 1, "num_frames": 93},
        {"name": "ball_s2", "prompt": "a ball falls", "image": "frame.png", "seed": 2},
        {"name": "pend_s1", "prompt": "a pendulum", "seed": 1},
    ]) + "\n")
    out = cli(url, "submit", str(jobs), "--owner", "alice-1", cwd=tmp_path)
    assert len(out.split()) == 6
    cli(url, "submit", str(jobs), "--owner", "bob-1", "--endpoints", "a", cwd=tmp_path)
    wait(lambda: all(j["status"] == "done" for j in q.jobs.values()))
    assert len(seen_a) + len(seen_b) == 6 and len(seen_a) >= 3  # bob's jobs only ran on "a"
    assert all(j["endpoint"] == "a" for j in q.jobs.values() if j["owner"] == "bob-1")
    alice = [s for s in seen_a + seen_b if b'"client": "alice-1"' in s]
    assert len(alice) == 3 and sum(b"PNGDATA" in s for s in alice) == 2  # the image went with both ball jobs

    got = cli(url, "fetch", "--owner", "alice-1", "--out", "clips", cwd=tmp_path)
    assert "downloaded 3" in got
    side = json.loads((tmp_path / "clips" / "ball_s1.json").read_text())
    assert side["seed"] == 1 and side["num_frames"] == 93 and side["owner"] == "alice-1" and side["endpoint"] in "ab"
    assert (tmp_path / "clips" / "ball_s1.mp4").read_bytes().startswith(b"MP4")
    assert "downloaded 0" in cli(url, "fetch", "--owner", "alice-1", "--out", "clips", cwd=tmp_path)
    assert "endpoint a" in cli(url, "status", cwd=tmp_path)
    stop.set(); srv.shutdown(); ea.shutdown(); eb.shutdown()


def test_queue_state_survives_a_restart_and_validates_input(tmp_path):
    root = tmp_path / "queue"
    q = qs.Queue(root, {"a": "http://127.0.0.1:9"})
    job = q.submit({"owner": "carol-1", "name": "x", "prompt": "p"})
    running = q.next_job("a")
    assert running["id"] == job["id"] and running["status"] == "running"
    q2 = qs.Queue(root, {"a": "http://127.0.0.1:9"})  # server restarted mid-job
    assert q2.jobs[job["id"]]["status"] == "pending"
    for bad in ({"owner": "c", "name": "../x", "prompt": "p"}, {"owner": "c", "name": "x", "prompt": ""},
                {"owner": "c", "name": "x", "prompt": "p", "endpoints": ["nope"]}):
        with pytest.raises(ValueError):
            q2.submit(bad)
    q2.cancel(job["id"])
    assert q2.next_job("a") is None


def test_fair_share_between_owners(tmp_path):
    q = qs.Queue(tmp_path / "queue", {"a": "x", "b": "y"})
    for i in range(5):
        q.submit({"owner": "hog", "name": f"h{i}", "prompt": "p"})
    q.submit({"owner": "small", "name": "s0", "prompt": "p"})
    first = q.next_job("a")
    second = q.next_job("b")
    assert first["owner"] == "hog" and second["owner"] == "small"  # small isn't stuck behind 5 hog jobs
