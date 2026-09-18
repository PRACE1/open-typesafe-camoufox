import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pytest

from src.capability.human_move import _get_model, human_loop, human_move, subsample_arc


class MockMouse:
    def __init__(self):
        self.calls = []

    async def move(self, x, y, **kwargs):
        self.calls.append((float(x), float(y)))


class MockPage:
    def __init__(self):
        self.mouse = MockMouse()


def test_subsample_arc_keeps_endpoints_and_count():
    xy = np.array([[0.0, 0.0], [1.0, 1.0], [2.0, 0.0], [3.0, 3.0], [4.0, 0.0]])
    out = subsample_arc(xy, 3)
    assert out.shape == (4, 2)
    np.testing.assert_allclose(out[0], xy[0])
    np.testing.assert_allclose(out[-1], xy[-1])


def test_subsample_arc_degenerate_single_point():
    xy = np.array([[5.0, 5.0]])
    out = subsample_arc(xy, 8)
    assert out.shape == (9, 2)
    np.testing.assert_allclose(out, np.tile(xy, (9, 1)))


def test_subsample_arc_monotonic_indices():
    xy = np.random.default_rng(0).random((50, 2)) * 100
    out = subsample_arc(xy, 9)
    assert out.shape == (10, 2)
    # arc-length even spacing: segment lengths roughly similar (no giant jumps)
    segs = np.linalg.norm(np.diff(out, axis=0), axis=1)
    assert segs.max() < 3 * segs.mean() + 1e-9


def test_human_move_near_coincident_is_single_exact_move():
    page = MockPage()
    ok = asyncio.run(human_move(page, (100.0, 100.0), (101.0, 100.5), vp={"width": 1280, "height": 800}))
    assert ok is True
    assert page.mouse.calls == [(101.0, 100.5)]


def test_human_move_long_is_mid_plus_end_and_bounded():
    model = _get_model()
    if model is None:
        pytest.skip("humanmovemouse not installed")
    page = MockPage()
    ok = asyncio.run(human_move(page, (100, 100), (700, 500), vp={"width": 1280, "height": 800}))
    assert ok is True
    calls = page.mouse.calls
    assert len(calls) == 2  # one lateral mid + the exact end
    assert calls[-1] == pytest.approx((700.0, 500.0), abs=0.01)
    for x, y in calls:
        assert 1.0 <= x <= 1279.0
        assert 1.0 <= y <= 799.0
    # the mid must be lateral to the chord, not right on the straight line
    sx, sy, ex, ey = 100.0, 100.0, 700.0, 500.0
    mx, my = calls[0]
    cross = (ex - sx) * (my - sy) - (ey - sy) * (mx - sx)
    assert abs(cross) > 1.0


def test_human_move_without_model_uses_deterministic_offset():
    import src.capability.human_move as hm

    real = hm._model
    try:
        hm._model = None
        hm._model_attempted = True
        page = MockPage()
        ok = asyncio.run(
            human_move(page, (100, 100), (700, 500), vp={"width": 1280, "height": 800})
        )
        assert ok is True  # deterministic offset still drives a long move
        assert len(page.mouse.calls) == 2
        assert page.mouse.calls[-1] == pytest.approx((700.0, 500.0), abs=0.01)
    finally:
        hm._model = real
        hm._model_attempted = False


def test_human_loop_shape_and_call_count():
    model = _get_model()
    if model is None:
        pytest.skip("humanmovemouse not installed")
    page = MockPage()
    start = asyncio.run(
        human_loop(page, 640.0, 400.0, 80.0, 50.0, hops=8, vp={"width": 1280, "height": 800})
    )
    assert start is not None
    calls = page.mouse.calls
    assert len(calls) == 8  # exactly `hops` arc points, no closing dispatch
    # every point sits exactly on the plain ellipse (no jitter, no wobble)
    for x, y in calls:
        dx = (x - 640.0) / 80.0
        dy = (y - 400.0) / 50.0
        assert dx * dx + dy * dy == pytest.approx(1.0, abs=1e-6)
    # closed revolution: first point at 0deg (right of center), spacing even
    assert calls[0] == pytest.approx((720.0, 400.0), abs=1e-6)
    assert start == pytest.approx((720.0, 400.0), abs=1e-6)


def test_human_loop_without_model_makes_no_calls():
    import src.capability.human_move as hm

    real = hm._model
    try:
        hm._model = None
        hm._model_attempted = True
        page = MockPage()
        start = asyncio.run(
            human_loop(page, 640.0, 400.0, 80.0, 50.0, vp={"width": 1280, "height": 800})
        )
        # plain ellipse math needs no model: 5 dispatches, all on the ellipse
        assert start is not None
        assert len(page.mouse.calls) == 5
    finally:
        hm._model = real
        hm._model_attempted = False