"""
CamoufoxPlatform: the single browser adapter (typesafe's `macos.py` equivalent).

Owns the async page handle and translates whitelisted verbs into humanized
Playwright actions. One instance per session.

An asyncio.Lock serializes all page access. Pydantic-AI's default
end_strategy='graceful' runs function tools in parallel within each segment;
since all browser verbs share one `page` object, concurrent access would
conflict. The lock lets the model emit multiple ToolCallParts per response
(no model round-trip between actions) while keeping page access safe.

The _navigating boolean flag is replaced by a TrackerMachine state machine
instance. safe_goto() sends GOTO (tracking -> navigating), runs page.goto(),
then sends NAV_DONE (navigating -> tracking). The framenavigated listener
sends EXTERNAL_NAV - ignored in navigating (our nav), fires in tracking ->
destroyed (recovery). The navigation race is structurally impossible.
"""

from __future__ import annotations

import asyncio

from ..capability.browser_actions import BrowserActionsMixin
from ..capability.cursor_tracking import CursorTrackingMixin
from ..capability.logging_utils import log


class CamoufoxPlatform(CursorTrackingMixin, BrowserActionsMixin):
    def __init__(self, page=None, *, humanize: bool | float = True) -> None:
        self._page = page
        self.humanize = humanize
        self._lock = asyncio.Lock()
        # Cursor events accumulated across page navigations. Each page.goto()
        # creates a new document context that destroys window.__cursorTracker,
        # so we harvest before navigation and merge into this buffer.
        self._accumulated_moves: list[dict] = []
        self._accumulated_clicks: list[dict] = []
        self._tracking_start_time: float | None = None
        # The _navigating boolean flag is replaced by the TrackerMachine.
        # Kept for backward compatibility with any code that reads it
        # directly; the tracker machine is the source of truth.
        self._navigating = False
        self._last_url: str | None = None
        # Tracker state machine instance. Initialized lazily in
        # _ensure_tracker_machine() so the xstate_statemachine import
        # doesn't happen at module load.
        self._tracker_machine = None
        self._tracker_interp = None
        # Generated-site guard (legacy video-agent compat): when set,
        # navigate() and scroll(to_element) were restricted to the
        # template-refs whitelist. Cursor movement stays unrestricted.
        self._generated_whitelist: dict[str, set[str]] | None = None
        self._generated_base_url: str | None = None
        # Idle-motion background loop. While a recording is active the cursor
        # should never freeze — even during LLM round-trips or gaps between
        # tool batches. This loop drifts the cursor in small random walks
        # whenever the page lock is free; any tool that acquires the lock
        # (scroll/click/navigate) preempts it instantly because the loop only
        # moves after a successful (non-blocking) lock acquire, then releases
        # it before sleeping.
        self._idle_motion_task: asyncio.Task | None = None
        self._idle_motion_paused = False
        self._stop_idle_motion = False
        self._idle_cur_x: float | None = None
        self._idle_cur_y: float | None = None
        # Last known in-browser cursor position (viewport coords). Kept in
        # sync by cursor_move(), the idle-motion loop, and the tool verbs;
        # used as the start point for human-trajectory moves so no gesture
        # ever teleports from (0,0).
        self._last_cursor_pos: tuple[float, float] | None = None

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("Camoufox page is not bound yet")
        return self._page

    async def cursor_move(self, x: float, y: float) -> None:
        """page.mouse.move with _last_cursor_pos bookkeeping.

        Every in-browser cursor move should go through this (or update the
        bookkeeping explicitly) so human-trajectory gestures always start
        from where the cursor actually is.
        """
        self._last_cursor_pos = (float(x), float(y))
        await asyncio.wait_for(self.page.mouse.move(float(x), float(y)), timeout=5.0)

    async def _ensure_tracker_machine(self):
        """Lazily initialize the TrackerMachine + Interpreter.

        Deferred so the xstate_statemachine import doesn't happen at module
        load. Called by safe_goto() and the framenavigated listener.

        Fail-soft: if the xstate_statemachine package is absent (not in
        pyproject deps), log once and run in boolean mode — the nav guard
        falls back to self._navigating, which safe_goto still maintains.
        """
        if self._tracker_interp is not None:
            return self._tracker_interp
        if getattr(self, "_tracker_machine_attempted", False):
            return None
        self._tracker_machine_attempted = True
        try:
            from ..capability.tracker_machine import TrackerMachine
            from xstate_statemachine import Interpreter
        except Exception as exc:
            log(f"[tracker] xstate_statemachine unavailable ({exc.__class__.__name__}); running in boolean mode")
            return None
        try:
            self._tracker_machine = TrackerMachine.create_machine()
            self._tracker_interp = await Interpreter(self._tracker_machine).start()
            return self._tracker_interp
        except Exception as exc:
            log(f"[tracker] machine init failed ({exc}); running in boolean mode")
            return None

    def _is_navigating_state(self) -> bool:
        """Check if the tracker machine is in the `navigating` state.

        Replaces the old `self._navigating` boolean check. Returns False if
        the tracker machine isn't initialized yet (pre-recording).
        """
        if self._tracker_interp is None:
            return self._navigating  # fallback to the boolean for pre-machine code
        return self._tracker_interp.matches("trackerMachine.navigating")

    async def _tracker_send(self, event: str) -> None:
        """Send an event to the tracker machine, if it's initialized."""
        if self._tracker_interp is not None:
            await self._tracker_interp.send(event)

    # -- legacy generated-site guard (compat; the whitelist verbs are gone) --

    def set_generated_whitelist(self, ref_bundle, generated_url: str) -> None:
        """Activate URL/selector guard for the generated template site phase."""
        import urllib.parse
        parsed = urllib.parse.urlparse(generated_url)
        self._generated_base_url = f"{parsed.scheme}://{parsed.netloc}"
        whitelist: dict[str, set[str]] = {}
        whitelist[parsed.path] = set()
        for page in ref_bundle.pages:
            if page.url_path and any(s.populated for s in page.sections):
                allowed_selectors = {s.selector for s in page.sections if s.populated}
                whitelist[page.url_path] = allowed_selectors
        self._generated_whitelist = whitelist
        log(f"[guard] generated-site whitelist: {len(whitelist)} pages, "
             f"{sum(len(v) for v in whitelist.values())} sections")

    def _is_on_generated_site(self) -> bool:
        if not self._generated_whitelist or not self._last_url:
            return False
        import urllib.parse
        parsed = urllib.parse.urlparse(self._last_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        return base == self._generated_base_url

    def _get_allowed_selectors_for_current_page(self) -> set[str]:
        import urllib.parse
        parsed = urllib.parse.urlparse(self._last_url or "")
        return self._generated_whitelist.get(parsed.path, set()) if self._generated_whitelist else set()

    # -- tabs: new-tab adoption, refresh, close-others (multi-tab caveats) --

    def tab_ids(self) -> set[int]:
        """Identity set of the context's current tabs (diff before/after click)."""
        try:
            return {id(p) for p in self.page.context.pages}
        except Exception:
            return set()

    async def tab_count(self) -> int:
        """Number of open tabs in this browser context."""
        try:
            return len(self.page.context.pages)
        except Exception:
            return 1

    async def adopt_new_tab(self, before_ids: set[int]) -> str | None:
        """Adopt the newest tab if the click just opened one. Returns its URL.

        Clicks on links/buttons often land in a fresh tab (target=_blank,
        popup). Without adoption the loop keeps perceiving the OLD tab and
        re-clicks the same element forever. Harvests the old document's
        buffer first, binds the new page, re-registers nav listeners, and
        restarts the tracker with the original start time so cursor.json
        stays continuous. None when no new tab appeared.
        """
        try:
            pages = list(self.page.context.pages)
        except Exception:
            return None
        fresh = [p for p in pages if id(p) not in before_ids]
        if not fresh:
            return None
        new_page = fresh[-1]
        try:
            await self._harvest_and_accumulate(quiet=True)
        except Exception as exc:  # noqa: BLE001
            log(f"[tabs] pre-adopt harvest: {exc}")
        old_url = self._last_url
        self._page = new_page
        try:
            await new_page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        self._last_url = new_page.url
        try:
            self._register_nav_listeners(new_page)
        except Exception as exc:  # noqa: BLE001
            log(f"[tabs] listener re-register: {exc}")
        try:
            await self._restart_tracker_on_new_page()
        except Exception as exc:  # noqa: BLE001
            log(f"[tabs] tracker restart: {exc}")
        log(f"[tabs] adopted new tab ({len(pages)} open): {old_url} -> {new_page.url}")
        return new_page.url

    async def settle_after_action(self, prev_url: str, before_ids: set[int],
                                  timeout_s: float = 4.0) -> tuple[str, str | None]:
        """Poll for the effect of a navigation-ish action (click, Enter).

        Replaces the blind fixed sleep: watches for a fresh tab (adopts it),
        a same-tab URL change (restarts the tracker on it), or nothing.
        Returns ("newtab"|"navigated"|"same", url-or-None). Breaks early on
        the first observed effect so fast pages cost ~0.5s, not the timeout.
        """
        import time as _time

        end = _time.monotonic() + max(0.5, timeout_s)
        while True:
            try:
                pages = list(self.page.context.pages)
            except Exception:
                pages = []
            fresh = [p for p in pages if id(p) not in before_ids]
            if fresh:
                adopted = await self.adopt_new_tab(before_ids)
                return ("newtab", adopted)
            try:
                cur = self.page.url
            except Exception:
                cur = prev_url
            if cur != prev_url:
                self._last_url = cur
                try:
                    await self._restart_tracker_on_new_page()
                except Exception as exc:  # noqa: BLE001
                    log(f"[tabs] tracker restart after nav: {exc}")
                return ("navigated", cur)
            if _time.monotonic() >= end:
                return ("same", None)
            await asyncio.sleep(0.5)

    async def close_other_tabs(self) -> int:
        """Close other tabs, keeping the current tab and its opener.

        The opener is kept deliberately: in this build a click-opened popup
        dies with its opener (verified live — closing example.com killed an
        adopted example.org tab), so closing it would strand the loop with
        no live tab. Returns closed count.
        """
        try:
            pages = list(self.page.context.pages)
        except Exception:
            return 0
        me = self._page
        try:
            opener = await me.opener()
        except Exception:  # noqa: BLE001
            opener = None
        keep = {id(me)}
        if opener is not None:
            keep.add(id(opener))
        closed = 0
        for p in pages:
            if id(p) in keep:
                continue
            try:
                await p.close()
                closed += 1
            except Exception as exc:  # noqa: BLE001
                log(f"[tabs] close failed: {exc}")
        try:
            self._last_url = self.page.url
        except Exception:  # noqa: BLE001
            pass
        if closed:
            kept = " (+opener kept)" if opener is not None else ""
            log(f"[tabs] closed {closed} other tab(s){kept}; current: {self._last_url}")
        # An uncommitted popup can die with its opener: if our tab didn't
        # survive, fall back to the last remaining tab (or report stranded).
        try:
            survivors = list(self.page.context.pages)
        except Exception:  # noqa: BLE001
            survivors = []
        if me not in survivors:
            if survivors:
                self._page = survivors[-1]
                self._last_url = self._page.url
                try:
                    self._register_nav_listeners(self._page)
                except Exception as exc:  # noqa: BLE001
                    log(f"[tabs] listener re-register: {exc}")
                try:
                    await self._restart_tracker_on_new_page()
                except Exception as exc:  # noqa: BLE001
                    log(f"[tabs] tracker restart: {exc}")
                log(f"[tabs] current tab died with its opener; fell back to {self._last_url}")
            else:
                log("[tabs] WARNING: no surviving tabs after close")
        return closed

    async def refresh_page(self) -> str:
        """Reload the current tab (stale/failed content). Harvests first.

        A reload timeout does NOT mean failure — if the URL changed or the
        DOM already looks usable, the reload succeeded from the loop's
        perspective. Only a genuinely blank/stuck page counts as a failure.
        """
        async with self._lock:
            try:
                await self._harvest_and_accumulate(quiet=True)
            except Exception as exc:  # noqa: BLE001
                log(f"[tabs] pre-refresh harvest: {exc}")
            prev_url = ""
            try:
                prev_url = self.page.url
            except Exception:  # noqa: BLE001
                pass
            reload_ok = False
            reload_err = ""
            try:
                await self.page.reload(wait_until="load", timeout=20000)
                reload_ok = True
            except Exception as exc:  # noqa: BLE001
                reload_err = str(exc)
                log(f"[tabs] reload timeout: {exc}")
                # Check whether the page is actually in a usable state
                # despite the timeout — navigation may have completed.
                try:
                    cur_url = self.page.url
                    if cur_url != prev_url:
                        reload_ok = True
                        log(f"[tabs] reload timed out but URL changed to {cur_url}")
                except Exception:  # noqa: BLE001
                    pass
                if not reload_ok:
                    # DOM might still be usable even if URL didn't change.
                    try:
                        text = await self.page.locator("body").inner_text(timeout=2000)
                        if len(text.strip()) > 50:
                            reload_ok = True
                            log("[tabs] reload timed out but body has usable text")
                    except Exception:  # noqa: BLE001
                        pass
            if reload_ok:
                self._last_url = self.page.url
                try:
                    await self._restart_tracker_on_new_page()
                except Exception as exc:  # noqa: BLE001
                    log(f"[tabs] tracker restart after refresh: {exc}")
                msg = f"refreshed {self._last_url}"
                log(msg)
                return msg
            msg = f"error: refresh failed: {reload_err}"
            log(msg)
            return msg

    async def go_back(self) -> str:
        """Browser-back in the current tab (result hopping: article -> SERP).

        Harvests first (back destroys the document), then restarts the
        tracker on the restored page. Empty history reports an error.
        A timeout is not an automatic failure: if the URL changed or the
        DOM is usable despite the timeout, the back-navigation succeeded.
        """
        async with self._lock:
            try:
                await self._harvest_and_accumulate(quiet=True)
            except Exception as exc:  # noqa: BLE001
                log(f"[tabs] pre-back harvest: {exc}")
            prev_url = ""
            try:
                prev_url = self.page.url
            except Exception:  # noqa: BLE001
                pass
            back_ok = False
            back_err = ""
            try:
                await self.page.go_back(wait_until="load", timeout=20000)
                back_ok = True
            except Exception as exc:  # noqa: BLE001
                back_err = str(exc)
                log(f"[tabs] go_back timeout: {exc}")
                # Same escape as refresh_page: a slow back that completed
                # just past the timeout is still a success.
                try:
                    cur_url = self.page.url
                    if cur_url != prev_url:
                        back_ok = True
                        log(f"[tabs] go_back timed out but URL changed to {cur_url}")
                except Exception:  # noqa: BLE001
                    pass
                if not back_ok:
                    try:
                        text = await self.page.locator("body").inner_text(timeout=2000)
                        if len(text.strip()) > 50:
                            back_ok = True
                            log("[tabs] go_back timed out but body has usable text")
                    except Exception:  # noqa: BLE001
                        pass
            if back_ok:
                self._last_url = self.page.url
                try:
                    await self._restart_tracker_on_new_page()
                except Exception as exc:  # noqa: BLE001
                    log(f"[tabs] tracker restart after back: {exc}")
                msg = f"went back to {self._last_url}"
                log(msg)
                return msg
            msg = f"error: back failed (empty history?): {back_err}"
            log(msg)
            return msg
