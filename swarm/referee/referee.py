"""Referee for planted cases: scores agents' measurements without revealing the answers.

The referee holds clips with known physical parameters (e.g. simulated drops
with g = 6.0). Agents download the clips from it, measure them with their tools
and submit estimates. The referee answers only with error bands and pass/fail,
rate-limits each agent, and posts every result to the board's #findings channel
so humans see it. It runs on any machine on the tailnet.

    python swarm/referee/referee.py --manifest planted.json --answers answers.json \
        --board-url http://<board-host>:8000

GET /planted lists the clips with a download URL each (GET /clips/<file>).

manifest.json (public; served at GET /planted):
    {"clips": [{"clip_id": "drop-017", "path": "<store>/planted/drop-017.mp4", "fps": 24,
                "params": ["g"], "notes": "ball diameter 0.10 m"}]}
answers.json (secret; keep it off the shared store):
    {"drop-017": {"g": 6.0}, "rest-03": {"end_state": "REST"}}

A string answer is a label (e.g. end_state REST/MOVING). Labels get no per-clip feedback, which would
give the answers away; the result has only totals (accuracy, Cohen's kappa, confusion counts), and
only when the agent labels at least --min-estimates of those clips (or all of them). UNCLEAR is
allowed and counts as scored but wrong.

POST /score body:
    {"agent": "alice-1", "method": "falltrack v2 @ 1a2b3c", "estimates": {"drop-017": {"g": 5.8}}}

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BANDS = ((0.05, "<5%"), (0.10, "5-10%"), (0.25, "10-25%"))
ABSTAIN = "UNCLEAR"


def band(rel_err: float) -> str:
    for limit, label in BANDS:
        if rel_err < limit:
            return label
    return ">25%"


class Referee:
    def __init__(
        self,
        manifest: dict[str, Any],
        answers: dict[str, dict[str, float]],
        *,
        tolerance: float = 0.10,
        pass_rate: float = 0.9,
        min_estimates: int = 10,
        limit_per_hour: int = 6,
        board_url: str | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.manifest = manifest
        self.answers = answers
        self.tolerance = tolerance
        self.pass_rate = pass_rate
        self.min_estimates = min_estimates
        self.limit_per_hour = limit_per_hour
        self.board_url = board_url.rstrip("/") if board_url else None
        self.log_path = log_path
        # Downloadable clip files by name, resolved from the manifest's paths.
        self.clip_files = {Path(c["path"]).name: Path(c["path"]) for c in manifest.get("clips", []) if c.get("path")}
        self._recent: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, agent: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self._lock:
            q = self._recent[agent]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.limit_per_hour:
                return False
            q.append(now)
            return True

    def score(self, agent: str, method: str, estimates: dict[str, dict[str, Any]]) -> dict[str, Any]:
        per_clip: dict[str, dict[str, str]] = {}
        scored = passed = 0
        ignored = []
        labels: dict[str, list[tuple[str, str]]] = defaultdict(list)  # param -> [(truth, answer)]
        for clip_id, params in estimates.items():
            truth = self.answers.get(clip_id)
            if truth is None or not isinstance(params, dict):
                ignored.append(clip_id)
                continue
            for name, value in params.items():
                if name not in truth:
                    continue
                if isinstance(truth[name], str):
                    labels[name].append((truth[name].upper(), str(value).strip().upper() or ABSTAIN))
                    continue
                try:
                    est = float(value)
                except (TypeError, ValueError):
                    continue
                true = float(truth[name])
                rel = abs(est - true) / max(abs(true), 1e-9)
                per_clip.setdefault(clip_id, {})[name] = band(rel)
                scored += 1
                passed += rel <= self.tolerance
        label_summary = {}
        for name, pairs in labels.items():
            needed = min(self.min_estimates, sum(1 for t in self.answers.values() if name in t))
            if len(pairs) < needed:
                label_summary[name] = {"labelled": len(pairs),
                                       "message": f"Label at least {needed} of the {name} clips; nothing was scored."}
                continue
            right = sum(t == a for t, a in pairs)
            scored += len(pairs)
            passed += right
            label_summary[name] = label_stats(pairs)
        rate = passed / scored if scored else 0.0
        verdict = "pass" if scored >= min(self.min_estimates, self._total_params()) and rate >= self.pass_rate else "fail"
        return {
            **({"labels": label_summary} if label_summary else {}),
            "agent": agent,
            "method": method,
            "estimates_scored": scored,
            "within_tolerance": passed,
            "pass_rate": round(rate, 3),
            "tolerance": self.tolerance,
            "verdict": verdict,
            "per_clip_error_band": per_clip,
            "ignored_clip_ids": ignored,
            "rule": f"pass = at least {self.min_estimates} estimates (or all) scored and "
                    f">= {self.pass_rate:.0%} within {self.tolerance:.0%} relative error "
                    f"(a label counts if it matches; {ABSTAIN} counts as wrong)",
        }

    def _total_params(self) -> int:
        return sum(len(v) for v in self.answers.values())

    def record(self, result: dict[str, Any]) -> None:
        stamped = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **result}
        if self.log_path:
            with self.log_path.open("a") as f:
                f.write(json.dumps(stamped) + "\n")
        if self.board_url:
            body = (
                f"Referee planted-case check for @{result['agent']}, method \"{result['method']}\": "
                f"{result['within_tolerance']}/{result['estimates_scored']} estimates within "
                f"{result['tolerance']:.0%} -> {result['verdict'].upper()}."
            )
            for name, st in result.get("labels", {}).items():
                if "correct" in st:
                    body += (f" {name}: {st['correct']}/{st['labelled']} correct, {st['abstained']} {ABSTAIN}, "
                             f"kappa {st['cohen_kappa']}.")
            try:
                post(f"{self.board_url}/api/messages", {"body": body, "channel": "findings", "type": "review"})
            except Exception as e:  # the agent still gets its answer
                print(f"could not post to board: {e!r}")


def label_stats(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Totals only: accuracy over all labels (UNCLEAR = wrong), accuracy and Cohen's kappa over the
    clips that got a real label, and confusion counts as {truth: {answer: count}}."""
    n = len(pairs)
    right = sum(t == a for t, a in pairs)
    decided = [(t, a) for t, a in pairs if a != ABSTAIN]
    confusion: dict[str, dict[str, int]] = {}
    for t, a in pairs:
        row = confusion.setdefault(t, {})
        row[a] = row.get(a, 0) + 1
    kappa = None
    if decided:
        m = len(decided)
        po = sum(t == a for t, a in decided) / m
        cats = {t for t, _ in decided} | {a for _, a in decided}
        pe = sum((sum(t == c for t, _ in decided) / m) * (sum(a == c for _, a in decided) / m) for c in cats)
        kappa = round((po - pe) / (1 - pe), 3) if pe < 1 else None
    return {"labelled": n, "correct": right, "abstained": n - len(decided),
            "accuracy": round(right / n, 3) if n else 0.0,
            "accuracy_when_labelled": round(sum(t == a for t, a in decided) / len(decided), 3) if decided else None,
            "cohen_kappa": kappa, "confusion": confusion}


def post(url: str, body: dict[str, Any]) -> None:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "X-Display-Name": "referee",
    })
    urllib.request.urlopen(req, timeout=10).read()


def make_handler(ref: Referee) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            if path in ("", "/planted"):
                base = f"http://{self.headers.get('Host', 'localhost')}/clips/"
                clips = [{**{k: v for k, v in c.items() if k != "path"}, "url": base + Path(c["path"]).name}
                         for c in ref.manifest.get("clips", [])]
                return self._send(200, {**ref.manifest, "clips": clips})
            if path.startswith("/clips/"):
                # Only files listed in the manifest are served; never the answers.
                clip = ref.clip_files.get(path.removeprefix("/clips/"))
                if clip is None or not clip.is_file():
                    return self._send(404, {"error": "not_found"})
                data = clip.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self._send(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.rstrip("/") != "/score":
                return self._send(404, {"error": "not_found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                agent, method, estimates = str(body["agent"]), str(body.get("method", "")), body["estimates"]
                assert isinstance(estimates, dict)
            except Exception:
                return self._send(400, {"error": "bad_request",
                                        "message": 'Send {"agent": ..., "method": ..., "estimates": {clip_id: {param: value}}}'})
            if not ref.allow(agent):
                return self._send(429, {"error": "rate_limited",
                                        "message": f"At most {ref.limit_per_hour} submissions per agent per hour."})
            result = ref.score(agent, method, estimates)
            ref.record(result)
            self._send(200, result)

        def log_message(self, fmt: str, *args: Any) -> None:
            print(f"{self.address_string()} {fmt % args}")

    return Handler


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--manifest", required=True)
    p.add_argument("--answers", required=True)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--board-url", help="post results to this board's #findings")
    p.add_argument("--tolerance", type=float, default=0.10, help="relative error that counts as correct")
    p.add_argument("--pass-rate", type=float, default=0.9)
    p.add_argument("--min-estimates", type=int, default=10)
    p.add_argument("--limit-per-hour", type=int, default=6)
    p.add_argument("--log", default="referee_submissions.jsonl")
    p.add_argument("--until", help="shut down at this ISO time with offset, e.g. 2026-11-01T08:00:00-05:00")
    args = p.parse_args()

    manifest = json.loads(Path(args.manifest).read_text())
    answers = json.loads(Path(args.answers).read_text())
    missing = [c["clip_id"] for c in manifest.get("clips", []) if c["clip_id"] not in answers]
    if missing:
        raise SystemExit(f"answers.json has no answers for: {missing}")
    ref = Referee(manifest, answers, tolerance=args.tolerance, pass_rate=args.pass_rate,
                  min_estimates=args.min_estimates, limit_per_hour=args.limit_per_hour,
                  board_url=args.board_url, log_path=Path(args.log))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(ref))
    print(f"referee on http://{args.host}:{args.port} with {len(answers)} planted clips")
    if args.until:
        end = datetime.fromisoformat(args.until)
        if end.tzinfo is None:
            raise SystemExit("--until needs a UTC offset, e.g. 2026-11-01T08:00:00-05:00")
        delay = max(0.0, (end - datetime.now(timezone.utc)).total_seconds())
        threading.Timer(delay, server.shutdown).start()
        print(f"shutting down at {end.isoformat()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
