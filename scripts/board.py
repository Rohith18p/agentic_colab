"""Act on the board as a human, from a script or for an explainer agent.

    python3 scripts/board.py --as "<name>" steer "Stop pursuing X; Y is ruled out (finding #123)"
    python3 scripts/board.py --as "<name>" post general "Please replicate #42 first" --type decision
    python3 scripts/board.py --as "<name>" task "Does fps change measured g?" --description "..."
    python3 scripts/board.py --as "<name>" pause alice-1 --reason "rethinking the plan"
    python3 scripts/board.py --as "<name>" resume alice-1
    python3 scripts/board.py --as "<name>" answer 57 "Use the H200 on box-b"
    python3 scripts/board.py --as "<name>" charter-replace compute "<old text>" "<new text>"

The board URL comes from --url or WORKSPACE_URL. Explainers act for a person and
pass --as "<person> (via explainer)", so every change shows who asked for it.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

STEERING_HEADER = "STEERING"


def call(base: str, who: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Display-Name": who},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path} failed ({e.code}): {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach the board at {base}: {e.reason}")


def add_steering(notes: str, line: str) -> str:
    """Append `- line` to the STEERING section of the notes (creating it if missing)."""
    lines = notes.splitlines()
    for i, text in enumerate(lines):
        if text.strip().startswith(STEERING_HEADER):
            j = i + 1
            while j < len(lines) and lines[j].strip().startswith("-"):
                j += 1
            section = [l for l in lines[i + 1:j] if l.strip() != "- (empty)"]
            return "\n".join(lines[:i + 1] + section + [f"- {line}"] + lines[j:])
    return f"{STEERING_HEADER} (humans edit this section; agents follow the latest version)\n- {line}\n\n{notes}".rstrip()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("WORKSPACE_URL", "http://127.0.0.1:8000"))
    p.add_argument("--as", dest="who", required=True, help='who is acting, e.g. "Nail" or "Nail (via explainer)"')
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("steer", help="add a line to the charter's STEERING notes")
    s.add_argument("line")
    s = sub.add_parser("post", help="post a message to a channel or task thread")
    s.add_argument("target", help='a channel name, or "task:<id>"')
    s.add_argument("body")
    s.add_argument("--type", default="human_input")
    s.add_argument("--reply-to", type=int)
    s = sub.add_parser("task", help="create an open task")
    s.add_argument("title")
    s.add_argument("--description", default="")
    s = sub.add_parser("pause", help="pause an agent")
    s.add_argument("agent")
    s.add_argument("--reason", default="")
    s = sub.add_parser("resume", help="resume an agent")
    s.add_argument("agent")
    s = sub.add_parser("answer", help="answer an agent's question from the Needs-human panel")
    s.add_argument("question_id", type=int)
    s.add_argument("body")
    s = sub.add_parser("charter-replace", help="replace text in one charter field (exact match)")
    s.add_argument("field", choices=["objective", "constraints", "libraries", "compute", "notes"])
    s.add_argument("old")
    s.add_argument("new")
    args = p.parse_args()
    base, who = args.url, args.who

    if args.cmd == "steer":
        charter = call(base, who, "GET", "/api/charter")
        notes = add_steering(charter.get("notes", ""), args.line)
        new = call(base, who, "PUT", "/api/charter", {
            **{k: charter.get(k, "") for k in ("objective", "constraints", "libraries", "compute")},
            "notes": notes, "base_version": charter["version"],
        })
        print(f"charter v{charter['version']} -> v{new['version']}: added steering line")
    elif args.cmd == "post":
        body = {"body": args.body, "type": args.type, "reply_to": args.reply_to}
        if args.target.startswith("task:"):
            body["task_id"] = int(args.target.split(":", 1)[1])
        else:
            body["channel"] = args.target.lstrip("#")
        msg = call(base, who, "POST", "/api/messages", body)
        print(f"posted message #{msg['id']}")
    elif args.cmd == "task":
        task = call(base, who, "POST", "/api/tasks", {"title": args.title, "description": args.description})
        print(f"created task #{task['id']}")
    elif args.cmd == "pause":
        call(base, who, "POST", f"/api/agents/{args.agent}/pause", {"reason": args.reason})
        print(f"paused {args.agent}")
    elif args.cmd == "resume":
        call(base, who, "POST", f"/api/agents/{args.agent}/resume")
        print(f"resumed {args.agent}")
    elif args.cmd == "charter-replace":
        charter = call(base, who, "GET", "/api/charter")
        if args.old not in charter.get(args.field, ""):
            sys.exit(f"'{args.old}' is not in the charter's {args.field}; nothing changed")
        fields = {k: charter.get(k, "") for k in ("objective", "constraints", "libraries", "compute", "notes")}
        fields[args.field] = fields[args.field].replace(args.old, args.new)
        new = call(base, who, "PUT", "/api/charter", {**fields, "base_version": charter["version"]})
        print(f"charter v{charter['version']} -> v{new['version']}: updated {args.field}")
    elif args.cmd == "answer":
        msg = call(base, who, "POST", "/api/messages", {"body": args.body, "type": "human_input", "reply_to": args.question_id})
        print(f"answered question #{args.question_id} (message #{msg['id']})")


if __name__ == "__main__":
    main()
