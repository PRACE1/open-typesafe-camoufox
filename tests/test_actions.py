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
    assert _run(_point_status(pg, 10, 10)) == "covered:div.cookie"
    pg2 = _FakePage(point="hit")
    assert _run(_point_status(pg2, 10, 10)) == "hit"


def _stub_find(monkeypatch, elements):
    """Stub the fresh-probe seam: _resolve_target re-probes per act."""
    import src.actions as _actions_mod

    async def _fake_find(platform):
        return list(elements)

    monkeypatch.setattr(_actions_mod, "find_elements", _fake_find)


def test_verified_center_hit_first_try(monkeypatch):
    _stub_find(monkeypatch, _pad([_el(1, label="Go")], 2))
    pg = _FakePage(point="hit")
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [_el(1, label="Go")], 1))
    assert verdict == "hit" and (px, py) == (60, 60)


def test_verified_center_retries_covered_then_hits(monkeypatch):
    _stub_find(monkeypatch, _pad([_el(1, label="Go")], 2))

    class _Flaky(_FakePage):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def evaluate(self, js, arg=None):
            self.calls += 1
            return "covered:div.banner" if self.calls == 1 else "hit"

    pg = _Flaky()
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [_el(1, label="Go")], 1))
    assert verdict == "hit"
    # center covered, first corner clear: two point checks, no re-resolve
    assert pg.calls == 2


def test_verified_center_gone_when_no_box(monkeypatch):
    _stub_find(monkeypatch, [])
    pg = _FakePage(None)
    assert _run(_verified_center(_FakePlatform(pg), [_el(1)], 1))[2] == "gone"


def test_verified_center_reports_persistent_cover(monkeypatch):
    _stub_find(monkeypatch, _pad([_el(1, label="Go")], 2))
    pg = _FakePage(point="covered:div.consent")
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [_el(1, label="Go")], 1))
    assert verdict == "covered:div.consent"


def test_verified_center_samples_corners_past_center_overlay(monkeypatch):
    wide = _el(1, label="Go", box=(0.1, 0.1, 0.2, 0.1))
    _stub_find(monkeypatch, _pad([wide], 2))

    class _Overlay(_FakePage):
        async def evaluate(self, js, arg=None):
            x = (arg or {}).get("x", 0)
            # center covered, left side clear
            return "covered:span.V9tjod" if x >= 200 else "hit"

    pg = _Overlay()
    px, py, verdict, live = _run(_verified_center(_FakePlatform(pg), [wide], 1))
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

from src.actions import (
    _challenge_kind,
    _remap_idx,
    _resolve_by_selector,
    _resolve_target,
    challenge_control,
    dispatch_verified_click,
    heal_target,
    probe_target,
    verify_for_dispatch,
)
from src.deps import ElementRef


def _el(idx, kind="link", label="More", box=(0.01, 0.05, 0.1, 0.05), sel=""):
    return ElementRef(idx=idx, kind=kind, label=label, ref=f"e{idx}", box=box, sel=sel)


class _FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeMouse:
    def __init__(self, fail=False):
        self.moves = []
        self.fail = fail

    async def click(self, px, py):
        if self.fail:
            raise RuntimeError("boom")
        self.moves.append(("click", px, py))

    async def move(self, x, y):
        self.moves.append(("move", x, y))

    async def down(self):
        self.moves.append(("down",))

    async def up(self):
        self.moves.append(("up",))


class _FakePage2(_FakePage):
    def __init__(self, box=None, point="hit"):
        super().__init__(box, point)
        self.mouse = _FakeMouse()
        self.url = "https://example.test/"


class _FakePlatform2:
    def __init__(self, page):
        self.page = page
        self._lock = _FakeLock()
        self.harvests = 0

    async def quiesce_idle_motion(self):
        return True

    def resume_idle_motion(self):
        pass

    async def _harvest_and_accumulate(self, quiet=True):
        self.harvests += 1

    async def _hover_and_highlight(self, box):
        pass

    def tab_ids(self):
        return ["t1"]

    async def settle_after_action(self, prev_url, before_ids):
        return ("settled", "")


def _box():
    return {"x": 10, "y": 20, "width": 100, "height": 40}


def test_probe_ok_through_covered(monkeypatch):
    els = [_el(2), _el(0), _el(1)]
    _stub_find(monkeypatch, [_el(0), _el(1), els[0]])
    p = _run(probe_target(_FakePlatform2(_FakePage2(point={"v": "hit", "k": "link"})), els, 2, "link"))
    assert p["status"] == "ok" and (p["px"], p["py"]) == (60, 60)
    assert p["label"] == "More" and p["live_kind"] == "link"
    p = _run(probe_target(_FakePlatform2(_FakePage2(point="covered:a.nav")), els, 2))
    assert p["status"] == "through"
    p = _run(probe_target(_FakePlatform2(_FakePage2(point="covered:div.banner")), els, 2))
    assert p["status"] == "covered"


def test_probe_unaimable_span_is_gone(monkeypatch):
    tall = _el(0, label="Card", box=(0.0, 0.0, 0.9, 3.0))
    _stub_find(monkeypatch, [tall])
    p = _run(probe_target(_FakePlatform2(_FakePage2()), [tall], 0))
    assert p["status"] == "gone"


SEL_BOX = {"x": 50, "y": 60, "width": 120, "height": 30}


class _FakeSelPage(_FakePage):
    """Serves selector hits + point verdicts by call shape."""

    def __init__(self, hit=None, point="hit", fail_resolve=False):
        super().__init__(point=point)
        self._hit = hit
        self._fail_resolve = fail_resolve
        self.keyboard = _FakeKeyboard()

    async def evaluate(self, js, arg=None):
        if isinstance(arg, str):  # RESOLVE_JS selector lookup
            if self._fail_resolve:
                raise RuntimeError("eval boom")
            return self._hit
        return self._point


def _sel_hit(kind="textarea", label="Search", box=None):
    b = box or [50, 60, 120, 30]
    return {"box": b, "kind": kind, "label": label, "text": "",
            "placeholder": "", "id": ""}


def test_resolve_by_selector_hit_and_miss():
    plat = _FakePlatform2(_FakeSelPage(hit=_sel_hit()))
    hit = _run(_resolve_by_selector(plat, 'textarea[name="q"]'))
    assert hit is not None and hit["box"] == SEL_BOX and hit["kind"] == "textarea"
    assert _run(_resolve_by_selector(plat, "")) is None
    plat2 = _FakePlatform2(_FakeSelPage(hit=None))
    assert _run(_resolve_by_selector(plat2, "e-nope")) is None
    plat3 = _FakePlatform2(_FakeSelPage(hit=_sel_hit(), fail_resolve=True))
    assert _run(_resolve_by_selector(plat3, "x")) is None
    plat4 = _FakePlatform2(_FakeSelPage(
        hit={"box": [0, 0, 5000, 30], "kind": "a", "label": ""}))
    assert _run(_resolve_by_selector(plat4, "x")) is None  # unaimable


def test_resolve_target_prefers_selector_over_nth(monkeypatch):
    """Same node via sel even when the nth slot holds another element —
    the Google-homepage shuffle that broke pure positional refs."""
    calls = []

    async def _counting_find(platform):
        calls.append(1)
        return [_el(0, kind="div", label="Other")]

    import src.actions as _actions_mod
    monkeypatch.setattr(_actions_mod, "find_elements", _counting_find)
    old = _el(5, kind="textarea", label="Search", sel='textarea[name="q"]')
    plat = _FakePlatform2(_FakeSelPage(hit=_sel_hit()))
    res = _run(_resolve_target(plat, [old], 5))
    assert res["status"] == "ok" and res["box"] == SEL_BOX
    assert calls == []  # no re-probe needed on the sel path


def test_resolve_target_selector_stale(monkeypatch):
    _stub_find(monkeypatch, [_el(0)])
    old = _el(0, kind="textarea", label="Search", sel='textarea[name="q"]')
    plat = _FakePlatform2(_FakeSelPage(hit=_sel_hit(kind="div", label="Other")))
    res = _run(_resolve_target(plat, [old], 0))
    assert res["status"] == "stale" and res["live_kind"] == "div"


def test_probe_via_selector_skips_nth(monkeypatch):
    import src.actions as _actions_mod

    async def _boom(platform):
        raise AssertionError("must not re-probe")

    monkeypatch.setattr(_actions_mod, "find_elements", _boom)
    old = _el(3, kind="link", label="More", sel="a#go",
              box=(0.01, 0.05, 0.1, 0.05))
    page = _FakeSelPage(
        hit={"box": [10, 40, 100, 40], "kind": "link", "label": "More",
             "text": "", "placeholder": "", "id": "go"})
    # point check: same node under the resolved center -> hit
    page._point = {"v": "hit", "k": "link"}
    p = _run(probe_target(_FakePlatform2(page), [old], 3, "link"))
    assert p["status"] == "ok" and p["live_kind"] == "link"


def test_remap_prefers_selector():
    fresh = [_el(0, label="Other"), _el(1, kind="link", label="More",
                                       sel="a#go")]
    assert _remap_idx(fresh, "missing-label", "link", "a#go") == 1
    assert _remap_idx(fresh, "more", "link", None) == 1
    assert _remap_idx(fresh, "", None, "a#go") == 1


def test_probe_stale_gone_missing(monkeypatch):
    els = [_el(2, kind="button"), _el(0), _el(1)]
    _stub_find(monkeypatch, [_el(0), _el(1), _el(2, kind="input")])
    p = _run(probe_target(
        _FakePlatform2(_FakePage2(point={"v": "hit", "k": "input"})),
        els, 2, "button"))
    assert p["status"] == "stale" and p["live_kind"] == "input"
    _stub_find(monkeypatch, [])
    p = _run(probe_target(_FakePlatform2(_FakePage2()), els, 2))
    assert p["status"] == "gone" and p["px"] is None
    p = _run(probe_target(_FakePlatform2(_FakePage2()), [_el(9)], 2))
    assert p["status"] == "gone"


def test_dispatch_click_settles_and_reports():
    plat = _FakePlatform2(_FakePage2(_box()))
    res = _run(dispatch_verified_click(plat, 60, 40))
    assert res["outcome"] == "settled" and plat.harvests == 1
    assert plat.page.mouse.moves == [("click", 60, 40)]
    plat2 = _FakePlatform2(_FakePage2(_box()))
    plat2.page.mouse.fail = True
    res = _run(dispatch_verified_click(plat2, 1, 1, harvest=False))
    assert res["outcome"] == "error" and plat2.harvests == 0


def test_remap_exact_case_insensitive_kind_filtered():
    fresh = [_el(7, kind="link", label="More results"), _el(3, kind="button", label="More Results")]
    assert _remap_idx(fresh, "more results") == 7
    assert _remap_idx(fresh, "MORE RESULTS", old_kind="button") == 3
    assert _remap_idx(fresh, "more result") is None  # no partials
    assert _remap_idx(fresh, "") is None


def test_heal_target_failsoft_without_browser(monkeypatch):
    import src.actions as _actions_mod

    async def _boom(platform):
        raise RuntimeError("no browser")

    monkeypatch.setattr(_actions_mod, "find_elements", _boom)
    fresh, idx = _run(heal_target(_FakePlatform2(_FakePage2(None)), "More"))
    assert (fresh, idx) == ([], None)


def test_heal_target_returns_fresh_list_for_retry(monkeypatch):
    moved = [_el(0, kind="link", label="Other", sel="a#x"),
             _el(1, kind="link", label="More", sel="a#go")]
    _stub_find(monkeypatch, moved)
    fresh, idx = _run(heal_target(_FakePlatform2(_FakePage2()),
                                  "More", "link", "a#go"))
    assert idx == 1 and fresh is not None and fresh[idx].sel == "a#go"


def test_challenge_classification():
    assert _challenge_kind(_el(1, label="I am not a robot captcha")) == "captcha"
    assert _challenge_kind(_el(1, label="Slide to verify")) == "slider"
    assert _challenge_kind(ElementRef(idx=1, kind="checkbox", label="Remember")) == "checkbox"
    assert _challenge_kind(_el(1, label="Next page")) == "none"


def _pad(elements, n):
    """Pad to n positional slots so fresh-probe nth-match resolves."""
    out = [_el(i) for i in range(n)]
    for e in elements:
        out[e.idx] = e
    return out


def test_challenge_captcha_escalates_and_none_refuses():
    plat = _FakePlatform2(_FakePage2())
    res = _run(challenge_control(plat, [_el(4, label="Solve captcha below")], 4))
    assert res.startswith("error:") and "escalating" in res
    res = _run(challenge_control(plat, [_el(5, label="Next")], 5))
    assert res.startswith("error:") and "not a checkbox/slider" in res
    res = _run(challenge_control(plat, [_el(5, label="Next")], 9))
    assert res.startswith("error:") and "gone" in res


def test_challenge_checkbox_and_slider_dispatch(monkeypatch):
    box = _el(6, kind="checkbox", label="I agree")
    _stub_find(monkeypatch, _pad([box], 7))
    plat = _FakePlatform2(_FakePage2())
    res = _run(challenge_control(plat, _pad([box], 7), 6))
    assert "checkbox toggled" in res
    slider = _el(7, label="Slide to unlock")
    _stub_find(monkeypatch, _pad([slider], 8))
    plat2 = _FakePlatform2(_FakePage2())
    res = _run(challenge_control(plat2, _pad([slider], 8), 7))
    assert "slider drag 8 steps" in res
    moves = [m[0] for m in plat2.page.mouse.moves]
    assert moves[0] == "move" and "down" in moves and moves[-1] == "up"
    assert moves.count("move") == 9  # initial + 8 steps


class _FakeKeyboard:
    def __init__(self, fail=False):
        self.presses = []
        self.fail = fail

    async def press(self, key):
        self.presses.append(key)
        if self.fail:
            raise RuntimeError("kbd boom")


class _FakePageSeq(_FakePage2):
    """Two-phase point verdicts: the first probe (up to 10 samples: 2 passes
    x 5 points) sees `first`, everything after sees `second`. Mirrors how
    _verified_center samples before returning."""

    def __init__(self, box, first, second):
        super().__init__(box, first)
        self._first = first
        self._second = second
        self._n = 0
        self.keyboard = _FakeKeyboard()

    async def evaluate(self, js, arg=None):
        self._n += 1
        return self._second if self._n > 10 else self._first


def test_verify_for_dispatch_clean_first_probe(monkeypatch):
    _stub_find(monkeypatch, [_el(0), _el(1), _el(2)])
    plat = _FakePlatform2(_FakePageSeq(_box(), "hit", "hit"))
    probe, dismissed = _run(verify_for_dispatch(plat, [_el(0), _el(1), _el(2)], 2, "link"))
    assert probe["status"] == "ok" and dismissed is False
    assert plat.page.keyboard.presses == []


def test_verify_for_dispatch_dismisses_cover_once(monkeypatch):
    _stub_find(monkeypatch, [_el(0), _el(1), _el(2)])
    plat = _FakePlatform2(_FakePageSeq(_box(), "covered:div.menu", "hit"))
    probe, dismissed = _run(verify_for_dispatch(plat, [_el(0), _el(1), _el(2)], 2))
    assert probe["status"] == "ok" and dismissed is True
    assert plat.page.keyboard.presses == ["Escape"]


def test_verify_for_dispatch_still_covered_stays_refusal(monkeypatch):
    _stub_find(monkeypatch, [_el(0), _el(1), _el(2)])
    plat = _FakePlatform2(_FakePageSeq(
        _box(), "covered:div.menu", "covered:div.menu"))
    probe, dismissed = _run(verify_for_dispatch(plat, [_el(0), _el(1), _el(2)], 2))
    assert probe["status"] == "covered" and dismissed is True
    assert plat.page.keyboard.presses == ["Escape"]  # exactly once


def test_verify_for_dispatch_escape_failure_keeps_probe(monkeypatch):
    _stub_find(monkeypatch, [_el(0), _el(1), _el(2)])
    page = _FakePageSeq(_box(), "covered:div.menu", "hit")
    page.keyboard.fail = True
    plat = _FakePlatform2(page)
    probe, dismissed = _run(verify_for_dispatch(plat, [_el(0), _el(1), _el(2)], 2))
    assert probe["status"] == "covered" and dismissed is False
