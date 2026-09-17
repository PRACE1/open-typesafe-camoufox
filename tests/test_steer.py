"""Steer-file + step-feed tests (pure, no browser)."""

import pytest

from src.run.agent_runner import parse_steer, read_steers, _READ_OK_RE, _format_elements


def test_parse_steer_stop():
    assert parse_steer("stop") == ("stop", None)
    assert parse_steer("  STOP  ") == ("stop", None)


def test_parse_steer_goto():
    assert parse_steer("goto https://x.com/home") == ("goto", "https://x.com/home")
    assert parse_steer("GOTO https://example.com") == ("goto", "https://example.com")
    # "goto" with no target degrades to a plain instruction (harmless)
    assert parse_steer("goto") == ("instruction", "goto")


def test_parse_steer_instruction():
    assert parse_steer("skip the phone prompt") == ("instruction", "skip the phone prompt")


def test_parse_steer_pause_resume():
    assert parse_steer("pause") == ("pause", None)
    assert parse_steer("RESUME") == ("resume", None)


def test_parse_steer_blank():
    assert parse_steer("") is None
    assert parse_steer("   ") is None


def test_read_steers_new_lines(tmp_path):
    f = tmp_path / "steer.txt"
    f.write_text("stop\ngo sign in\n", encoding="utf-8")
    items, consumed = read_steers(str(f), 0)
    assert items == [("stop", None), ("instruction", "go sign in")]
    assert consumed == 2
    # nothing new
    items2, consumed2 = read_steers(str(f), consumed)
    assert items2 == [] and consumed2 == 2
    # append one
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("goto https://x.com\n")
    items3, consumed3 = read_steers(str(f), consumed2)
    assert items3 == [("goto", "https://x.com")] and consumed3 == 3


def test_read_steers_missing_file(tmp_path):
    items, consumed = read_steers(str(tmp_path / "nope.txt"), 0)
    assert items == [] and consumed == 0


def test_read_ok_regex_matches_real_line():
    line = "frame (40x12 braille) -> jev x=0.531 y=0.719 click=1 conf=0.82"
    m = _READ_OK_RE.search(line)
    assert m
    assert m.group(1) == "40" and m.group(2) == "12"
    assert m.group(3) == "0.531" and m.group(4) == "0.719" and m.group(6) == "0.82"


def test_read_ok_regex_rejects_error_line():
    assert _READ_OK_RE.search("error capturing frame: boom") is None
    assert _READ_OK_RE.search("frame blank after text rendering; skipping Jev call") is None


def test_format_elements_empty():
    assert _format_elements([]) == "(no actionable elements found on this page)"


def test_format_elements_rows():
    els = [
        {"idx": 0, "kind": "input", "type": "text", "label": "Email", "placeholder": "", "text": "", "id": "", "cx": 0.5, "cy": 0.4},
        {"idx": 1, "kind": "button", "type": "submit", "label": "Sign in", "placeholder": "", "text": "Sign in", "id": "", "cx": 0.5, "cy": 0.55},
    ]
    out = _format_elements(els)
    assert '[0] input (text) "Email" @ 0.500,0.400' in out
    assert '[1] button (submit) "Sign in" @ 0.500,0.550' in out


def test_format_elements_falls_back_to_placeholder_and_text():
    els = [
        {"idx": 0, "kind": "input", "type": "password", "label": "", "placeholder": "Password", "text": "", "id": "", "cx": 0.1, "cy": 0.2},
        {"idx": 1, "kind": "link", "type": "", "label": "", "placeholder": "", "text": "Forgot", "id": "", "cx": 0.3, "cy": 0.6},
    ]
    out = _format_elements(els)
    assert '"Password"' in out
    assert '"Forgot"' in out