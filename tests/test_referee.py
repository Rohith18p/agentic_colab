"""The planted-case referee scores without revealing answers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "referee", Path(__file__).resolve().parent.parent / "swarm" / "referee" / "referee.py"
)
referee = importlib.util.module_from_spec(spec)
spec.loader.exec_module(referee)


def make(**kw):
    answers = {f"drop-{i}": {"g": 6.0} for i in range(10)}
    manifest = {"clips": [{"clip_id": c, "params": ["g"]} for c in answers]}
    return referee.Referee(manifest, answers, **kw)


def test_scores_bands_and_verdict_without_leaking_truth():
    ref = make()
    good = ref.score("a", "m1", {f"drop-{i}": {"g": 6.1} for i in range(10)})
    assert good["verdict"] == "pass" and good["within_tolerance"] == 10
    assert good["per_clip_error_band"]["drop-0"] == {"g": "<5%"}
    assert "6.0" not in json.dumps(good)

    bad = ref.score("a", "m2", {**{f"drop-{i}": {"g": 9.8} for i in range(10)}, "nope": {"g": 1}})
    assert bad["verdict"] == "fail" and bad["per_clip_error_band"]["drop-3"] == {"g": ">25%"}
    assert bad["ignored_clip_ids"] == ["nope"]

    too_few = ref.score("a", "m3", {"drop-0": {"g": 6.0}})
    assert too_few["verdict"] == "fail"


def test_rate_limit_per_agent():
    ref = make(limit_per_hour=2)
    assert ref.allow("a", now=1000) and ref.allow("a", now=1001)
    assert not ref.allow("a", now=1002)
    assert ref.allow("b", now=1002)
    assert ref.allow("a", now=1000 + 3601)


def test_labels_score_totals_only_and_unclear_counts_wrong():
    answers = {f"rest-{i:02d}": {"end_state": "REST" if i % 2 else "MOVING"} for i in range(12)}
    answers["drop-0"] = {"g": 6.0}
    ref = referee.Referee({"clips": []}, answers)
    perfect = ref.score("a", "m", {c: dict(t) for c, t in answers.items() if c.startswith("rest-")})
    assert perfect["verdict"] == "pass" and perfect["labels"]["end_state"]["cohen_kappa"] == 1.0
    assert "rest-00" not in json.dumps(perfect)  # no per-clip feedback for labels

    guesses = {c: {"end_state": "rest"} for c in answers if c.startswith("rest-")}
    guesses["rest-00"] = {"end_state": "UNCLEAR"}
    res = ref.score("a", "m", guesses)["labels"]["end_state"]
    assert res["correct"] == 6 and res["abstained"] == 1 and res["accuracy"] == 0.5
    assert res["confusion"] == {"MOVING": {"UNCLEAR": 1, "REST": 5}, "REST": {"REST": 6}}

    probe = ref.score("a", "m", {"rest-00": {"end_state": "MOVING"}})
    assert probe["verdict"] == "fail" and "correct" not in probe["labels"]["end_state"]
