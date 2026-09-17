"""Tab-race tests — stale tab listeners must not touch shared state (offline fakes)."""

import asyncio

from src.browser.camoufox import CamoufoxPlatform


class _Frame:
    def __init__(self, url):
        self.url = url


class _Page:
    def __init__(self, url):
        self.url = url
        self.main_frame = _Frame(url)
        self.handlers = {}

    def on(self, ev, cb):
        self.handlers[ev] = cb


def _platform(page):
    p = CamoufoxPlatform.__new__(CamoufoxPlatform)
    p._page = page
    p._last_url = page.url
    p._navigating = False
    p._tracker_interp = None
    p._tracker_machine_attempted = True
    p._tracking_start_time = None
    p._accumulated_moves = []
    p._accumulated_clicks = []
    p._rel_time = lambda: "t+0.0s"  # noqa: E731
    return p


def test_stale_tab_navigation_ignored():
    old = _Page("https://old.example/")
    p = _platform(old)
    p._register_nav_listeners(old)
    # Adopt: rebind to the new tab (new listeners registered there too).
    new = _Page("https://new.example/")
    p._page = new
    p._last_url = new.url
    p._register_nav_listeners(new)
    # The old tab navigates late (redirect settling, bfcache, prerender).
    old.main_frame.url = "https://old.example/other"
    old.handlers["framenavigated"](old.main_frame)
    assert p._last_url == "https://new.example/"


def test_current_tab_navigation_still_tracked():
    async def _run():
        new = _Page("https://new.example/")
        p = _platform(new)
        p._register_nav_listeners(new)
        new.main_frame.url = "https://new.example/next"
        new.handlers["framenavigated"](new.main_frame)
        await asyncio.sleep(0.1)
        return p

    p = asyncio.run(_run())
    assert p._last_url == "https://new.example/next"


def test_stale_dialog_untouched_current_dismissed():
    class _Dialog:
        def __init__(self, type):
            self.type = type
            self.message = "m"
            self.accepted = False
            self.dismissed = False

        async def accept(self):
            self.accepted = True

        async def dismiss(self):
            self.dismissed = True

    async def _run():
        old = _Page("https://old.example/")
        p = _platform(old)
        p._register_nav_listeners(old)
        new = _Page("https://new.example/")
        p._page = new
        p._register_nav_listeners(new)
        stale = _Dialog("confirm")
        old.handlers["dialog"](stale)
        live = _Dialog("confirm")
        new.handlers["dialog"](live)
        await asyncio.sleep(0.1)
        return stale, live

    stale, live = asyncio.run(_run())
    assert stale.dismissed is False and stale.accepted is False
    assert live.dismissed is True
