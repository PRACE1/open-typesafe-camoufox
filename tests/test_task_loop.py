"""Task-loop tests: step schema, placeholder resolution, secret masking."""

import pytest
from pydantic import ValidationError

from jev_agent import JevStep
from jev_tools import MoveCursorAction
from src.capability.jev_actions import _mask, _resolve_placeholders


def test_step_defaults():
    s = JevStep(x=0.5, y=0.5)
    assert s.done is False and s.click is False and s.type_text is None
    assert s.reasoning == ""


def test_step_reasoning_streamable():
    s = JevStep(x=0.5, y=0.5, reasoning="click the password field — centered input")
    assert s.reasoning == "click the password field — centered input"


def test_move_cursor_action_element_mode():
    a = MoveCursorAction(x=0.5, y=0.5, element=2, type_text="{TWITTER_PASSWORD}", key="Enter")
    assert a.element == 2 and a.key == "Enter"


def test_move_cursor_action_element_bounds():
    with pytest.raises(ValidationError):
        MoveCursorAction(x=0.5, y=0.5, element=-1)
    assert MoveCursorAction(x=0.1, y=0.2).element is None


def test_jev_step_element_defaults():
    assert JevStep(x=0.5, y=0.5).element is None
    assert JevStep(x=0.5, y=0.5, element=3).element == 3
    with pytest.raises(ValidationError):
        JevStep(x=0.5, y=0.5, element=-5)


def test_jev_step_goto():
    assert JevStep(x=0.5, y=0.5).goto is None
    s = JevStep(x=0.5, y=0.5, goto="https://www.google.com")
    assert s.goto == "https://www.google.com"


def test_step_rejects_bad_key():
    with pytest.raises(ValidationError):
        JevStep(x=0.5, y=0.5, key="F5")


def test_step_accepts_enter():
    assert JevStep(x=0.1, y=0.2, key="Enter").key == "Enter"


def test_step_rejects_out_of_range():
    with pytest.raises(ValidationError):
        JevStep(x=2.0, y=0.5)


def test_move_accepts_type_and_key():
    a = MoveCursorAction(x=0.3, y=0.4, click=True, type_text="{TWITTER_USERNAME}", key="Tab")
    assert a.type_text == "{TWITTER_USERNAME}" and a.key == "Tab"


def test_placeholders_resolve_from_env(monkeypatch):
    monkeypatch.setenv("TWITTER_USERNAME", "someuser")
    assert _resolve_placeholders("{TWITTER_USERNAME}") == "someuser"
    assert _resolve_placeholders("no placeholders") == "no placeholders"
    assert _resolve_placeholders("{MISSING_VAR_XYZ}") == "{MISSING_VAR_XYZ}"


def test_mask_hides_values():
    assert _mask("secret123") == "<9 chars masked>"
    assert _mask(None) == "none"
    assert "secret" not in _mask("secret123")


def test_done_requires_note():
    """done with no note is rejected — the planner must quote the outcome."""
    from src.run.agent_runner import _handle_done

    pending, reason = _handle_done(JevStep(x=0.5, y=0.5), 3)
    assert pending is False and reason is None

    accepted, reason = _handle_done(JevStep(x=0.5, y=0.5, done=True, note="fact: the moon has an exosphere"), 3)
    assert accepted is True and reason is None

    rejected, reason = _handle_done(JevStep(x=0.5, y=0.5, done=True), 4)
    assert rejected is False and reason is not None
    assert "DONE REJECTED" in reason

    rejected_ws, reason_ws = _handle_done(JevStep(x=0.5, y=0.5, done=True, note="   "), 5)
    assert rejected_ws is False and reason_ws is not None
