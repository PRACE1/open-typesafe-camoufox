"""Highlight tests — cursory ring + settle fallback (offline fakes)."""

import asyncio

import pytest

from src.capability.browser_actions import BrowserActionsMixin


class _FakeMouse:
    def __init__(self, fail=False):
        self.moves = []
        self.fail = fail

    async def move(self, x, y, **kwargs):
        if self.fail:
            raise RuntimeError("mouse boom")
        self.moves.append((round(x, 1), round(y, 1)))


class _FakePage:
    def __init__(self, fail=False):
        self.mouse = _FakeMouse(fail)
        self.viewport_size = {"width": 1000, "height": 800}


class _Mixin(BrowserActionsMixin):
    def __init__(self, page):
        self.page = page
        self._last_cursor_pos = None


def _run(coro):
    return asyncio.run(coro)


def _box():
    return {"x": 100.0, "y": 200.0, "width": 200.0, "height": 80.0}


def test_hover_and_highlight_rings_and_settles():
    page = _FakePage()
    mix = _Mixin(page)
    _run(mix._hover_and_highlight(_box()))
    assert len(page.mouse.moves) >= 4  # ring arcs, bounded dispatches
    assert len(page.mouse.moves) <= 16
    assert mix._last_cursor_pos == (200.0, 240.0)  # center
    xs = [m[0] for m in page.mouse.moves]
    assert min(xs) < 200.0 < max(xs)  # ring spans both sides of center


def test_highlight_failure_settles_directly(monkeypatch):
    # browser_actions lazy-imports from .human_move at call time, so
    # module-attribute patches take effect without touching the method.
    import src.capability.human_move as hm

    async def _boom(*a, **k):
        return None

    async def _ok_move(page, start, end, **k):
        await page.mouse.move(end[0], end[1])
        return True

    monkeypatch.setattr(hm, "cursory_loop", _boom)
    monkeypatch.setattr(hm, "cursory_move", _ok_move)
    page = _FakePage()
    mix = _Mixin(page)
    _run(mix._hover_and_highlight(_box()))
    assert mix._last_cursor_pos == (200.0, 240.0)
    assert page.mouse.moves == [(200.0, 240.0)]


def test_cursory_move_and_loop_contract():
    from src.capability.human_move import (
        _subsample_cursory,
        cursory_loop,
        cursory_move,
    )

    assert _subsample_cursory([(0.0, 0.0)], 8) == [(0.0, 0.0)]
    pts = [(float(i), 0.0) for i in range(100)]
    assert len(_subsample_cursory(pts, 8)) == 8

    page = _FakePage()
    assert _run(cursory_move(page, (0.0, 0.0), (500.0, 400.0))) is True
    assert len(page.mouse.moves) <= 8

    page2 = _FakePage()
    start = _run(cursory_loop(page2, 200.0, 240.0, 80.0, 32.0))
    assert start is not None
    assert len(page2.mouse.moves) >= 4


def test_mouse_move_pins_steps_kwarg():
    """Regression: omitted steps hangs Camoufox's input pipeline
    (1s -> 4s -> 8s -> timeout); every dispatch must pin steps=1."""
    from src.capability.human_move import mouse_move

    seen = {}

    class _M:
        async def move(self, x, y, **kwargs):
            seen.update(kwargs)

    class _P:
        mouse = _M()
        viewport_size = {"width": 1000, "height": 800}

    _run(mouse_move(_P(), 1.0, 2.0))
    assert seen == {"steps": 1}


def test_cursory_move_surfacing_failure():
    from src.capability.human_move import cursory_move

    page = _FakePage(fail=True)
    assert _run(cursory_move(page, (0.0, 0.0), (10.0, 10.0))) is False
