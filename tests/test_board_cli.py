"""scripts/board.py: the human-action CLI explainers use."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "board.py"
spec = importlib.util.spec_from_file_location("board_cli", SCRIPT)
board_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board_cli)


def run(url: str, *args: str) -> str:
    out = subprocess.run([sys.executable, str(SCRIPT), "--url", url, "--as", "Nail (via explainer)", *args],
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_add_steering_keeps_the_rest_of_the_notes():
    notes = "STEERING (humans edit this section)\n- (empty)\n\nSTARTING CLUE\n- g is 0.45"
    once = board_cli.add_steering(notes, "Drop the pendulum line")
    assert once == "STEERING (humans edit this section)\n- Drop the pendulum line\n\nSTARTING CLUE\n- g is 0.45"
    twice = board_cli.add_steering(once, "Prioritise replication")
    assert twice.splitlines()[1:3] == ["- Drop the pendulum line", "- Prioritise replication"]
    assert board_cli.add_steering("", "x").startswith("STEERING")


def test_cli_acts_as_the_human_with_attribution(live_server):
    ws = live_server.ws
    ws.register_agent("alice-1")
    run(live_server.url, "steer", "Focus on free fall first")
    charter = ws.read_charter()
    assert "- Focus on free fall first" in charter["notes"] and charter["edited_by"] == "Nail (via explainer)"

    run(live_server.url, "post", "general", "@alice-1 please replicate #2", "--type", "decision")
    msg = ws.list_messages(channel="general")[-1]
    assert msg["author_name"] == "Nail (via explainer)" and msg["mentions"] == ["alice-1"]

    assert "created task" in run(live_server.url, "task", "Does fps change g?")
    run(live_server.url, "pause", "alice-1", "--reason", "rethink")
    assert ws.get_control_state("alice-1")["paused"]
    run(live_server.url, "resume", "alice-1")
    assert not ws.get_control_state("alice-1")["paused"]

    question = ws.ask_human("alice-1", "Which GPU should I use?")
    run(live_server.url, "answer", str(question["id"]), "The H200 on box-b")
    assert ws.needs_human() == []


def test_charter_replace_edits_one_field(live_server):
    ws = live_server.ws
    ws.update_charter("lead", {"compute": "Referee: http://laptop:8090", "notes": "keep me"})
    run(live_server.url, "charter-replace", "compute", "http://laptop:8090", "http://board:8090")
    charter = ws.read_charter()
    assert charter["compute"] == "Referee: http://board:8090" and charter["notes"] == "keep me"
    out = subprocess.run([sys.executable, str(SCRIPT), "--url", live_server.url, "--as", "x", "charter-replace",
                          "compute", "not there", "y"], capture_output=True, text=True)
    assert out.returncode != 0 and ws.read_charter()["version"] == charter["version"]
