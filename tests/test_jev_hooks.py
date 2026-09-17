"""Tests for the Jev hook helpers (pure parts only — no network, no git state)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.jev_hooks import _excerpt, _heuristic_score, main


def test_heuristic_score_bounds_and_signals(tmp_path=None):
    assert _heuristic_score("clean idiomatic code\npass\n") < 0.2
    smelly = "TODO fix this\nHACK workaround\nexcept Exception:\n    pass\nprint(x)\n"
    assert 0.2 <= _heuristic_score(smelly) <= 0.49
    assert _heuristic_score("x" * 10000) <= 0.49


def test_excerpt_truncation(tmp_path):
    p = tmp_path / "sample.py"
    p.write_text("\n".join(f"line {i}" for i in range(300)), encoding="utf-8")
    head, nlines = _excerpt(str(p))
    assert nlines == 300
    assert len(head) <= 4000
    assert head.startswith("line 0")
    missing, n0 = _excerpt(str(tmp_path / "nope.py"))
    assert (missing, n0) == ("", 0)


def test_main_usage_and_off_switch():
    assert main([]) == 2
    assert main(["bogus"]) == 2
    os.environ["JEV_HOOKS_OFF"] = "1"
    try:
        assert main(["pre-push"]) == 0
    finally:
        del os.environ["JEV_HOOKS_OFF"]
