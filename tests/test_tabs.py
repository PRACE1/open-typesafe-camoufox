"""Tab tests — adoption, close-others, refresh, count (offline fakes)."""

import asyncio

from src.browser.camoufox import CamoufoxPlatform
from src.decide import Kind


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakePage:
    def __init__(self, url, ctx=None, opener=None):
        self.url = url
        self.context = ctx
        self._opener = opener
        self.closed = False
        self.reloaded = False
        self.went_back = False
        self.fronted = False

    async def bring_to_front(self):
        self.fronted = True

    async def close(self):
        self.closed = True

    async def reload(self, **kwargs):
        self.reloaded = True

    async def go_back(self, **kwargs):
        self.went_back = True

    async def opener(self):
        return self._opener


class FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _platform(page):
    p = CamoufoxPlatform.__new__(CamoufoxPlatform)
    p._page = page
    p._last_url = page.url
    p._lock = FakeLock()
    p._accumulated_moves = []
    p._accumulated_clicks = []
    calls = []

    async def _harvest(*a, **k):
        calls.append("harvest")

    def _register(pg):
        calls.append("register")

    async def _restart():
        calls.append("restart")

    p._harvest_and_accumulate = _harvest
    p._register_nav_listeners = _register
    p._restart_tracker_on_new_page = _restart
    p._calls = calls
    return p


def test_tab_ids_and_count():
    old = FakePage("https://old.example")
    new = FakePage("https://new.example")
    ctx = FakeContext([old, new])
    old.context = ctx
    p = _platform(old)
    assert p.tab_ids() == {id(old), id(new)}
    assert asyncio.run(p.tab_count()) == 2


def test_tab_count_fallback_without_context():
    p = _platform(FakePage("https://x.example", ctx=None))
    p._page.context = None
    # context None -> pages attr raises -> fallback 1
    assert asyncio.run(p.tab_count()) == 1


def test_adopt_new_tab_rebinds_and_restarts():
    old = FakePage("https://old.example")
    new = FakePage("https://new.example")
    ctx = FakeContext([old, new])
    old.context = ctx
    new.context = ctx
    p = _platform(old)
    adopted = asyncio.run(p.adopt_new_tab({id(old)}))
    assert adopted == "https://new.example"
    assert p._page is new
    assert p._last_url == "https://new.example"
    assert new.fronted is True
    assert p._calls == ["harvest", "register", "restart"]


def test_adopt_new_tab_none_when_no_popup():
    old = FakePage("https://old.example")
    ctx = FakeContext([old])
    old.context = ctx
    p = _platform(old)
    assert asyncio.run(p.adopt_new_tab({id(old)})) is None
    assert p._page is old
    assert p._calls == []


def test_adopt_new_tab_picks_newest():
    old = FakePage("https://old.example")
    n1 = FakePage("https://n1.example")
    n2 = FakePage("https://n2.example")
    ctx = FakeContext([old, n1, n2])
    for pg in (old, n1, n2):
        pg.context = ctx
    p = _platform(old)
    assert asyncio.run(p.adopt_new_tab({id(old)})) == "https://n2.example"


def test_close_other_tabs():
    me = FakePage("https://me.example")
    b = FakePage("https://b.example")
    c = FakePage("https://c.example")
    ctx = FakeContext([me, b, c])
    for pg in (me, b, c):
        pg.context = ctx
    p = _platform(me)
    assert asyncio.run(p.close_other_tabs()) == 2
    assert me.closed is False and b.closed and c.closed
    assert p._last_url == "https://me.example"


def test_close_other_tabs_keeps_opener():
    opener = FakePage("https://opener.example")
    me = FakePage("https://me.example", opener=opener)
    stranger = FakePage("https://s.example")
    ctx = FakeContext([opener, me, stranger])
    for pg in (opener, me, stranger):
        pg.context = ctx
    p = _platform(me)
    assert asyncio.run(p.close_other_tabs()) == 1
    assert me.closed is False and opener.closed is False and stranger.closed


def test_refresh_page_reloads_and_restarts():
    pg = FakePage("https://r.example")
    pg.context = FakeContext([pg])
    p = _platform(pg)
    msg = asyncio.run(p.refresh_page())
    assert pg.reloaded is True
    assert msg == "refreshed https://r.example"
    assert p._calls == ["harvest", "restart"]


def test_go_back_navigates_history():
    pg = FakePage("https://r.example/page2")
    pg.context = FakeContext([pg])
    p = _platform(pg)
    msg = asyncio.run(p.go_back())
    assert pg.went_back is True
    assert msg == "went back to https://r.example/page2"
    assert p._calls == ["harvest", "restart"]


def test_settle_detects_newtab():
    async def _run():
        a = FakePage("https://a.example/")
        ctx = FakeContext([a])
        a.context = ctx
        p = _platform(a)
        before = p.tab_ids()

        async def _opener():
            await asyncio.sleep(0.2)
            b = FakePage("https://b.example/")
            b.context = ctx
            ctx.pages.append(b)

        t = asyncio.create_task(_opener())
        out = await p.settle_after_action(a.url, before, timeout_s=2.0)
        await t
        return out, p

    (outcome, info), p = asyncio.run(_run())
    assert outcome == "newtab" and info == "https://b.example/"
    assert p._page.url == "https://b.example/"


def test_settle_detects_sametab_nav():
    async def _run():
        a = FakePage("https://a.example/")
        b = FakePage("https://a.example/next")
        ctx = FakeContext([a, b])
        a.context = ctx
        b.context = ctx
        p = _platform(a)
        before = p.tab_ids()
        p._page = b  # navigation swapped the document, same tab set
        return await p.settle_after_action(a.url, before, timeout_s=1.0), p

    (outcome, info), p = asyncio.run(_run())
    assert outcome == "navigated" and info == "https://a.example/next"
    assert p._last_url == "https://a.example/next"
    assert "restart" in p._calls


def test_settle_same_on_timeout():
    a = FakePage("https://a.example/")
    ctx = FakeContext([a])
    a.context = ctx
    p = _platform(a)
    assert asyncio.run(p.settle_after_action(a.url, p.tab_ids(), timeout_s=0.3)) == ("same", None)


def test_new_kinds_decode():
    from src.decide import _decode
    from src.deps import ElementRef
    els = [ElementRef(idx=0, kind="link")]
    sites = ["https://a.example"]
    raw = {"answers": {"kind": {"choice": "refresh", "confidence": 0.8},
                       "item": {"choice": "0"}, "site": {"choice": "0"}}}
    assert _decode(raw, els, sites).kind == Kind.REFRESH
    raw["answers"]["kind"]["choice"] = "close_others"
    assert _decode(raw, els, sites).kind == Kind.CLOSE_OTHERS
    raw["answers"]["kind"]["choice"] = "back"
    assert _decode(raw, els, sites).kind == Kind.BACK
    raw["answers"]["kind"]["choice"] = "press_escape"
    assert _decode(raw, els, sites).kind == Kind.PRESS_ESCAPE
