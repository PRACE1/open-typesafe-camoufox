"""Jev schema tests — mirrors video-agent tool_schema_test.py for 2 tools."""

import pytest
from pydantic import ValidationError

from jev_tools import JevSolverTool, MoveCursorAction, ReadFrameAction


def test_read_frame_defaults():
    a = ReadFrameAction()
    assert a.fps == 3.0
    assert a.format == "jpeg"


def test_read_frame_rejects_bad_fps():
    with pytest.raises(ValidationError):
        ReadFrameAction(fps=60.0)


def test_move_requires_xy():
    with pytest.raises(ValidationError):
        MoveCursorAction()  # type: ignore[call-arg]


def test_move_rejects_out_of_range():
    with pytest.raises(ValidationError):
        MoveCursorAction(x=1.5, y=0.5)


def test_move_humanize_defaults_true():
    assert MoveCursorAction(x=0.5, y=0.5).humanize is True


def test_solver_tool_single_action():
    t = JevSolverTool(move_cursor=MoveCursorAction(x=0.1, y=0.2, click=True))
    assert t.move_cursor and t.move_cursor.click is True
    assert t.read_frame is None
