"""Action tests — failure accounting + point verification (offline fakes)."""

import asyncio

from src.actions import (
    _inspect_point, _point_status, _refresh_snapshot, _sample_points,
    _verified_center, action_failed, may_click_through, stale_mismatch,
)


def test_action_failed_convention():
    assert action_failed("error: element #3 has no bounding box") is True
    assert action_failed("error: element #3 target covered:div.banner — skipping click (no dispatch)") is True
    assert action_failed("error: nav failed: boom") is True
    assert action_failed("error: refresh failed: boom") is True
    assert action_failed("element #3 scroll+circle+click at (0.5,0.5) humanize=true") is False
    assert action_failed("navigated to https://a.example/") is False
    assert action_failed("key=Enter") is False
    assert action_failed(None) is False
    assert action_failed("") is False


class _FakeLocator:
    def __init__(self, box=None, scrolls=None):
        self._box = box
        self.scrolls = scrolls if scrolls is not None else []

    async def scroll_into_view_if_needed(self, timeout=None):
        self.scrolls.append(1)

    async def bounding_box(self):
        return self._box


class _FakePage:
    def __init__(self, box=None, point="hit"):
        self._loc = _FakeLocator(box)
        self._point = point
        self.viewport_size = {"width": 1000, "height": 800}

    def locator(self, sel):
        self._sel = sel
        return self

    @property
    def first(self):
        return self._loc

    async def evaluate(self, js, arg=None):
        return self._point


class _FakePlatform:
    def __init__(self, page):
        self.page = page


def _run(coro):
    return asyncio.run(coro)


def test_point_status_passthrough():
    pg = _FakePage(point="covered:div.cookie")
    assert _run(_point_status(pg, '[data-jev="1"]', 10, 10)) == "covered:div.cookie"
    pg2 = _FakePage(point="hit")
    assert _run(_point_status(pg2, '[data-jev="1"]', 10, 10)) == "hit"


def test_verified_center_hit_first_try():
    box = {"x": 100, "y": 100, "width": 200, "height": 40}
    pg = _FakePage(box, point="hit")
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [], 1))
    assert verdict == "hit" and (px, py) == (200, 120)
    assert len(pg._loc.scrolls) == 1


def test_verified_center_retries_covered_then_hits():
    box = {"x": 100, "y": 100, "width": 200, "height": 40}

    class _Flaky(_FakePage):
        def __init__(self):
            super().__init__(box)
            self.calls = 0

        async def evaluate(self, js, arg=None):
            self.calls += 1
            return "covered:div.banner" if self.calls == 1 else "hit"

    pg = _Flaky()
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [], 1))
    assert verdict == "hit"
    # sampling hits a clear corner within the first scroll (no re-scroll)
    assert len(pg._loc.scrolls) == 1 and pg.calls == 2


def test_verified_center_gone_when_no_box():
    pg = _FakePage(None)
    assert _run(_verified_center(_FakePlatform(pg), [], 1))[2] == "gone"


def test_verified_center_reports_persistent_cover():
    box = {"x": 100, "y": 100, "width": 200, "height": 40}
    pg = _FakePage(box, point="covered:div.consent")
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [], 1))
    assert verdict == "covered:div.consent"
    assert len(pg._loc.scrolls) == 2


def test_verified_center_samples_corners_past_center_overlay():
    box = {"x": 100, "y": 100, "width": 200, "height": 100}

    class _Overlay(_FakePage):
        async def evaluate(self, js, arg=None):
            x = (arg or {}).get("x", 0)
            # center covered, left side clear
            return "covered:span.V9tjod" if x >= 200 else "hit"

    pg = _Overlay(box)
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [], 1))
    assert verdict == "hit" and px < 200


def test_stale_mismatch():
    assert stale_mismatch(None, "link") is False
    assert stale_mismatch("link", "") is False
    assert stale_mismatch("link", "link") is False
    assert stale_mismatch("link", "textarea") is True
    assert stale_mismatch("textarea", "input") is True


def test_sample_points_center_first_and_clamped():
    box = {"x": 100, "y": 100, "width": 200, "height": 100}
    from src.actions import _sample_points
    pts = _sample_points(box, {"width": 1000, "height": 800})
    assert pts[0] == (200, 150) and len(pts) == 5
    tiny = _sample_points({"x": 0, "y": 0, "width": 2, "height": 2},
                          {"width": 1000, "height": 800})
    assert tiny[0] == (1, 1) and len(tiny) <= 5
    assert all(0 <= x < 1000 and 0 <= y < 800 for x, y in tiny)


def test_may_click_through_only_actionable_cover():
    assert may_click_through("hit") is False
    assert may_click_through("gone") is False
    assert may_click_through("void") is False
    assert may_click_through("covered:a.PMDqCb") is True
    assert may_click_through("covered:button.submit") is True
    assert may_click_through("covered:span.V9tjod") is False
    assert may_click_through("covered:div.consent") is False


def test_refresh_snapshot_digest_and_empty():
    class _SnapPage:
        url = "https://a.example/"
        viewport_size = {"width": 1000, "height": 800}

        async def evaluate(self, js):
            if "innerText" in js:
                return "Hello world results here"
            return [{"idx": 0, "kind": "link", "text": "R",
                     "cx": 0.1, "cy": 0.1}]

    class _SnapPlatform:
        def __init__(self):
            self.page = _SnapPage()

    digest = _run(_refresh_snapshot(_SnapPlatform()))
    assert digest == "fresh(1 elements, 24ch :: Hello world results here)"

    class _EmptyPage(_SnapPage):
        async def evaluate(self, js):
            return [] if "innerText" not in js else ""

    class _EmptyPlatform:
        def __init__(self):
            self.page = _EmptyPage()

    assert _run(_refresh_snapshot(_EmptyPlatform())) == ""
