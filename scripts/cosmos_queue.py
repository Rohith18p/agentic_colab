#!/usr/bin/env python3
"""Submit Cosmos clip jobs to the swarm's shared queue, then come back for the results.

The queue (scripts/cosmos_queue_server.py, announced in #compute) feeds every free Cosmos
endpoint. Jobs and finished clips live on the queue's machine, so your shift can end, your
client can die, and nothing is lost: run `fetch` in a later shift.

  python3 scripts/cosmos_queue.py submit jobs.jsonl --owner <agent-name> [--endpoints a100,gb10]
  python3 scripts/cosmos_queue.py status [--owner <agent-name>]       # your jobs, or the whole queue
  python3 scripts/cosmos_queue.py fetch --owner <agent-name> --out out  # download finished clips
  python3 scripts/cosmos_queue.py cancel <job-id> ...

jobs.jsonl uses the same format as tools/cosmos_local/client.py: one JSON object per line with
"name" and "prompt" (text, or a path to a .json caption), and optional "image" (path to the input
frame), width, height, num_frames, fps, steps, guidance, flow_shift, seed, negative_prompt,
extra_params. `fetch` writes <name>.mp4 and <name>.json (the sidecar: every setting, the endpoint
that ran it, timings) and skips clips already downloaded.

A line with "kind": "probe_loss" runs the denoising-loss probe instead of a clip: it needs "video" (path to
an mp4) and optional "image" and "probe" (the settings object of POST /v1/probe/loss: sigmas, seeds, variants...).
`fetch` writes only <name>.json for it (the probe's result). Clip jobs that capture activations (extra_params.internals)
list the server's artifact ids in the sidecar's "artifact_ids". The queue also relays the endpoint's own sidecar (what
actually ran: sigmas, executed hooks, noise, server revision) and the artifacts; `fetch` writes them as
<name>.server.json and <name>.<artifact_id>.npz.

The queue URL comes from --url or COSMOS_QUEUE_URL. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://board.tail0c526a.ts.net:8030"
PARAMS = ("kind", "probe", "width", "height", "num_frames", "fps", "steps", "guidance", "flow_shift", "seed", "negative_prompt",
          "extra_params")


def call(url: str, method: str = "GET", body: dict | None = None, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read() if raw else json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = str(e)
        raise SystemExit(f"queue error ({e.code}): {msg}")
    except urllib.error.URLError as e:
        raise SystemExit(f"can't reach the queue at {url}: {e.reason}")


def text(x):
    """A prompt may be a path to a JSON caption, as in client.py."""
    if isinstance(x, str) and x.endswith(".json") and os.path.exists(x):
        return json.dumps(json.load(open(x)))
    return x


def submit(a) -> None:
    endpoints = [e for e in (a.endpoints or "").split(",") if e]
    n = 0
    for line in open(a.jobs):
        if not line.strip():
            continue
        j = json.loads(line)
        body = {"owner": a.owner, "name": j["name"], "prompt": text(j["prompt"]),
                **{k: (text(j[k]) if k == "negative_prompt" else j[k]) for k in PARAMS if k in j}}
        if endpoints:
            body["endpoints"] = endpoints
        if j.get("video"):
            body["video_b64"] = base64.b64encode(Path(j["video"]).read_bytes()).decode()
            body["video_name"] = os.path.basename(j["video"])
        if j.get("image"):
            body["image_b64"] = base64.b64encode(Path(j["image"]).read_bytes()).decode()
            body["image_name"] = os.path.basename(j["image"])
        r = call(f"{a.url}/jobs", "POST", body)
        print(f"{r['id']}  {j['name']}")
        n += 1
    print(f"submitted {n} job(s). Later: python3 scripts/cosmos_queue.py fetch --owner {a.owner} --out <dir>",
          file=sys.stderr)


def status(a) -> None:
    if a.owner:
        for j in call(f"{a.url}/jobs?owner={a.owner}")["jobs"]:
            extra = j.get("error") or ""
            print(f"{j['id']}  {j['status']:9}  {j.get('endpoint') or '-':8}  {j['name']}  {extra[:100]}")
        return
    s = call(f"{a.url}/status")
    for name, e in s["endpoints"].items():
        h = e.get("health") or {}
        state = "down" if e.get("ok") is False else ("queue job " + e["job"] if e.get("job")
                                                     else "busy (direct)" if h.get("busy") else "free")
        print(f"endpoint {name:10} {state:24} done {e['done']}  failed {e['failed']}  {e['url']}")
    for owner, c in sorted(s["owners"].items()):
        print(f"owner {owner:16} " + "  ".join(f"{k} {v}" for k, v in sorted(c.items())))


def fetch(a) -> None:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    got = left = 0
    for j in call(f"{a.url}/jobs?owner={a.owner}")["jobs"]:
        if j["status"] != "done":
            left += j["status"] in ("pending", "running")
            if j["status"] == "failed":
                print(f"failed: {j['name']} ({j['id']}): {j.get('error')}", file=sys.stderr)
            continue
        mp4 = out / f"{j['name']}.mp4"
        probe = j.get("kind") == "probe_loss"  # no clip: the result is the JSON
        if (out / f"{j['name']}.json" if probe else mp4).exists():
            continue
        (out / f"{j['name']}.json").write_text(json.dumps(j.get("sidecar", {}), indent=1))
        if probe:
            got += 1
            continue
        mp4.write_bytes(call(f"{a.url}/jobs/{j['id']}/mp4", raw=True))
        for rel in j.get("sidecar", {}).get("relayed", []):  # the endpoint's own sidecar and artifacts
            dest = out / (f"{j['name']}.server.json" if rel == "server_sidecar.json" else f"{j['name']}.{Path(rel).name}")
            dest.write_bytes(call(f"{a.url}/jobs/{j['id']}/files/{rel}", raw=True))
        got += 1
    print(f"downloaded {got} result(s) to {out}; {left} still pending or running")


def cancel(a) -> None:
    for jid in a.ids:
        print(jid, call(f"{a.url}/jobs/{jid}", "DELETE")["status"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("COSMOS_QUEUE_URL", DEFAULT_URL))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("submit")
    p.add_argument("jobs")
    p.add_argument("--owner", required=True)
    p.add_argument("--endpoints", help="comma-separated endpoint names to allow (default: any)")
    p = sub.add_parser("status")
    p.add_argument("--owner")
    p = sub.add_parser("fetch")
    p.add_argument("--owner", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("cancel")
    p.add_argument("ids", nargs="+")
    a = ap.parse_args()
    a.url = a.url.rstrip("/")
    {"submit": submit, "status": status, "fetch": fetch, "cancel": cancel}[a.cmd](a)


if __name__ == "__main__":
    main()
