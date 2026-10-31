"""Push the swarm charter (swarm/charter/*.md) to a running workspace.

    python scripts/seed_swarm.py --url http://<board-host>:8000 --as <your name> [--kickoff]

Refuses to publish while any field still contains a "<FILL" placeholder,
unless --allow-fill is given (useful for a dry run). Uses only the standard
library, so it runs anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

FIELDS = ("objective", "constraints", "libraries", "compute", "notes")
ROOT = Path(__file__).resolve().parent.parent

KICKOFF = """\
Swarm start. Everyone: read the charter and your playbook, then post a one-line plan here
(type proposal) before claiming or creating a task. First priority is measuring tools that
pass the planted cases; nothing counts until a tool does. Findings go in #findings.
Humans steer through the charter notes and #decisions."""


def request(method: str, url: str, who: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "X-Display-Name": who,
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url} failed: {e.code} {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach {url}: {e.reason}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8000", help="board base URL")
    p.add_argument("--as", dest="who", default="lead", help="your display name on the board")
    p.add_argument("--charter-dir", default=str(ROOT / "swarm" / "charter"))
    p.add_argument("--allow-fill", action="store_true", help="publish even with <FILL placeholders")
    p.add_argument("--kickoff", action="store_true", help="also post the kickoff message to #general")
    args = p.parse_args()

    base = args.url.rstrip("/")
    fields = {f: (Path(args.charter_dir) / f"{f}.md").read_text().strip() for f in FIELDS}
    unfilled = [f"{f}: {line.strip()}" for f, text in fields.items() for line in text.splitlines() if "<FILL" in line]
    if unfilled and not args.allow_fill:
        print("These placeholders still need filling (or pass --allow-fill):", *unfilled, sep="\n  ")
        sys.exit(1)

    current = request("GET", f"{base}/api/charter", args.who)
    new = request("PUT", f"{base}/api/charter", args.who, {**fields, "base_version": current["version"]})
    print(f"charter: version {current['version']} -> {new['version']}")
    if unfilled:
        print(f"warning: published with {len(unfilled)} unfilled placeholders")
    if args.kickoff:
        msg = request("POST", f"{base}/api/messages", args.who, {"body": KICKOFF, "channel": "general", "type": "decision"})
        print(f"kickoff posted to #general (message {msg['id']})")


if __name__ == "__main__":
    main()
