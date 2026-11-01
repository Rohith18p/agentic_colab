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


def test_probe_loss_job_posts_to_probe_endpoint_and_stores_json(tmp_path):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"busy": False, "waiting": 0}).encode())

        def do_POST(self):
            data = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, data))
            if self.path == "/v1/probe/loss":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"rows": [{"sigma": 0.5, "loss": 1.25}]}).encode())
            else:  # a clip job that captured activations: the artifact ids come back in a header
                self.send_response(200)
                self.send_header("X-Artifact-Ids", "act_ab12,act_cd34")
                self.end_headers()
                self.wfile.write(b"MP4")

    ep = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=ep.serve_forever, daemon=True).start()
    q, stop, srv, url = start_queue(tmp_path / "queue", {"a": f"http://127.0.0.1:{ep.server_address[1]}"})
    (tmp_path / "clip.mp4").write_bytes(b"VIDEODATA")
    (tmp_path / "first.png").write_bytes(b"PNGDATA")
    jobs = tmp_path / "jobs.jsonl"
    jobs.write_text("\n".join(json.dumps(j) for j in [
        {"name": "loss_fwd", "kind": "probe_loss", "prompt": "a ball", "video": "clip.mp4", "image": "first.png",
         "probe": {"sigmas": [0.5], "seeds": [1]}},
        {"name": "clip1", "prompt": "a ball"},
    ]) + "\n")
    cli(url, "submit", str(jobs), "--owner", "dave-1", cwd=tmp_path)
    wait(lambda: all(j["status"] == "done" for j in q.jobs.values()))
    probe_posts = [d for p, d in seen if p == "/v1/probe/loss"]
    assert len(probe_posts) == 1 and b"VIDEODATA" in probe_posts[0] and b"PNGDATA" in probe_posts[0]
    assert b'"sigmas": [0.5]' in probe_posts[0] and b'name="settings"' in probe_posts[0]
    assert "downloaded 2" in cli(url, "fetch", "--owner", "dave-1", "--out", "res", cwd=tmp_path)
    res = json.loads((tmp_path / "res" / "loss_fwd.json").read_text())
    assert res["rows"][0]["loss"] == 1.25 and res["queue"]["owner"] == "dave-1"
    assert not (tmp_path / "res" / "loss_fwd.mp4").exists()  # a probe has no clip
    assert json.loads((tmp_path / "res" / "clip1.json").read_text())["artifact_ids"] == ["act_ab12", "act_cd34"]
    assert "downloaded 0" in cli(url, "fetch", "--owner", "dave-1", "--out", "res", cwd=tmp_path)
    with pytest.raises(ValueError):
        q.submit({"owner": "d", "name": "x", "prompt": "p", "kind": "probe_loss"})  # no video
    with pytest.raises(ValueError):
        q.submit({"owner": "d", "name": "x", "prompt": "p", "kind": "bogus"})
    stop.set(); srv.shutdown(); ep.shutdown()


def test_option_jobs_only_go_to_endpoints_that_support_them(tmp_path):
    # An older server accepts extra_params.noise/internals but ignores them (#4989), so the queue must not send
    # such a job there: it waits for an endpoint whose /health lists the feature.
    q = qs.Queue(tmp_path, {"old": "http://old", "new": "http://new"})
    plain = q.submit({"owner": "a", "name": "plain", "prompt": "p"})
    noisy = q.submit({"owner": "b", "name": "noisy", "prompt": "p", "extra_params": {"noise": {"device": "cpu"}}})
    assert qs.needs(q.jobs[noisy["id"]]) == {"noise"} and qs.needs(q.jobs[plain["id"]]) == set()
    got = q.next_job("old", set())
    assert got["name"] == "plain"
    assert q.next_job("old", set()) is None  # the noise job is not given to the old server
    assert q.next_job("new", {"noise", "internals"})["name"] == "noisy"
