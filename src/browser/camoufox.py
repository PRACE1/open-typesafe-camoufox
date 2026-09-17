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
