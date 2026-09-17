"""Tests for the Jev hook helpers (pure parts only — no network, no git state)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.jev_hooks import (
    ISSUE_KINDS,
    _excerpt,
    _heuristic_score,
    _parse_opts,
    _subsample,
    _windows,
    main,
)


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


def test_windows_are_1_indexed_inclusive():
    lines = [f"line {i}" for i in range(1, 121)]
    out = _windows(lines, width=50)
    assert [(s, e) for s, e, _ in out] == [(1, 50), (51, 100), (101, 120)]
    assert out[0][2].startswith("line 1")
    assert out[-1][2].endswith("line 120")
    assert _windows([]) == []
    single = _windows(["only"])
    assert single == [(1, 1, "only")]


def test_subsample_even_and_bounded():
    items = list(range(100))
    out = _subsample(items, 10)
    assert len(out) == 10
    assert out[0] == 0 and out[-1] == 90
    assert _subsample(items, 200) == items
    assert _subsample(items, 0) == items
    assert _subsample(items, 1) == [0]


def test_parse_opts_defaults_and_overrides():
    d = _parse_opts([], 0.75)
    assert (d["threshold"], d["line_floor"], d["line_hot"],
            d["window"], d["strict"], d["no_lines"]) == \
        (0.75, 0.35, 0.6, 50, False, False)
    d2 = _parse_opts(["--threshold", "0.9", "--line-floor", "0.5",
                      "--line-hot", "0.8", "--window", "25",
                      "--strict", "--no-lines"], 0.75)
    assert (d2["threshold"], d2["line_floor"], d2["line_hot"],
            d2["window"], d2["strict"], d2["no_lines"]) == \
        (0.9, 0.5, 0.8, 25, True, True)
    d3 = _parse_opts(["--window", "3", "--threshold", "bogus"], 0.75)
    assert d3["window"] == 10  # clamped minimum
    assert d3["threshold"] == 0.75  # bad value ignored
    d4 = _parse_opts(["--files", "a.py", "b.py", "--threshold", "0.9"], 0.75)
    assert d4["files"] == ["a.py", "b.py"] and d4["threshold"] == 0.9
    assert _parse_opts(["--files"], 0.75)["files"] == []


def test_issue_kinds_cover_none_option():
    assert "none-of-these" in ISSUE_KINDS
    assert len(ISSUE_KINDS) >= 6
    for key, desc in ISSUE_KINDS.items():
        assert isinstance(key, str) and isinstance(desc, str) and desc.strip()
