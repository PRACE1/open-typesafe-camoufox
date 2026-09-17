"""Mission driver tests — coverage accumulation (offline, no browser)."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.run.run import MISSION_MAX_SESSIONS, _covered_urls


def _wire(tmp_path, urls):
    path = os.path.join(str(tmp_path), "wire.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for i, u in enumerate(urls):
            f.write(json.dumps({"n": i + 1, "url": u}) + "\n")
    return str(tmp_path)


def test_covered_urls_merges_deduped_in_order(tmp_path):
    run_dir = _wire(tmp_path, ["https://a.example/", "https://b.example/",
                               "https://a.example/"])
    out = _covered_urls(run_dir, ["https://z.example/"])
    assert out == ["https://z.example/", "https://a.example/",
                   "https://b.example/"]


def test_covered_urls_tolerates_missing_and_corrupt(tmp_path):
    assert _covered_urls(str(tmp_path), ["https://z.example/"]) == [
        "https://z.example/"]
    bad = os.path.join(str(tmp_path), "wire.jsonl")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("not json\n")
        f.write(json.dumps({"n": 1}) + "\n")
        f.write(json.dumps({"n": 2, "url": "https://ok.example/"}) + "\n")
    assert _covered_urls(str(tmp_path), []) == ["https://ok.example/"]


def test_mission_session_cap_is_bounded():
    assert MISSION_MAX_SESSIONS == 10
