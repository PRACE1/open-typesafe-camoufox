"""
CursorTrackingMixin: everything to do with the JS mouse tracker's lifecycle —
idle-motion drift, injection/re-injection across navigations, harvesting,
and the safe_goto() wrapper that navigates without losing the cursor buffer.

Mixed into CamoufoxCapability (see capability.py) alongside
BrowserActionsMixin. Not meant to be instantiated on its own — it assumes
the instance attributes set up in CamoufoxCapability.__init__ (self._page,
self._lock, self._accumulated_moves, etc).
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from .._log_sink import kv as _kv
from .logging_utils import log, log_verbose
from .cursor_tracker_script import CURSOR_TRACKER_INIT_SCRIPT


class CursorTrackingMixin:
    def _rel_time(self) -> str:
        """Elapsed time relative to cursor-tracking start, for correlating logs."""
        if self._tracking_start_time is None:
            return "pre-record"
        import time as _time

        return f"t+{(_time.time() * 1000.0 - self._tracking_start_time) / 1000.0:.1f}s"

    async def start_idle_motion(self) -> None:
        """Launch the background idle-cursor loop so the cursor never freezes.

        During LLM round-trips (the dt gap between ModelRequestNode and
        CallToolsNode) and between tool batches, no tool holds the page lock,
        so the cursor would sit perfectly still — which reads as frozen in the
        recording. This loop drifts the cursor in small random walks whenever
        the lock is free. Any tool (scroll/click/navigate) acquires the lock
        and the loop's next iteration immediately yields because its
        `asyncio.Lock.acquire()` is non-blocking; the tool runs to completion,
        releases the lock, and the loop resumes drifting.

        Safe to call before the page is fully ready — the loop no-ops if
        `_page` or `_tracking_start_time` is unset.
        """
        if self._idle_motion_task is not None and not self._idle_motion_task.done():
            return  # already running
        self._stop_idle_motion = False
        self._idle_motion_task = asyncio.create_task(self._idle_motion_loop())
        _kv("capability:cursor:idle", status="started")

    async def stop_idle_motion(self) -> None:
        """Stop the idle-cursor loop and let the last move finish."""
        self._stop_idle_motion = True
        task = self._idle_motion_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=2.0)
            except asyncio.TimeoutError:
                task.cancel()
            except Exception:
                pass
        self._idle_motion_task = None
        _kv("capability:cursor:idle", status="stopped")

    def resume_idle_motion(self) -> None:
        """Re-arm the idle drift loop after a tool action finishes."""
        self._idle_motion_paused = False

    async def quiesce_idle_motion(self, settle: float = 0.35) -> bool:
        """Pause the idle drift loop and let in-flight animations drain.

        The page lock serializes our CALLS but not animations already queued
        in the browser's input pipeline: with humanize=True each idle drift
        move fans out into ~100 intermediate mousemove dispatches. A tool move
        issued right after an idle move lands behind that queue and stalls
        (measured: per-move latency climbs 1.2s -> 2.5s+ until the 10s tool
        timeout trips). Pause + short settle fixes it (probe: 0/8 stalls).

        Returns True if the loop was actually paused (caller must then call
        resume_idle_motion() when the action completes).
        """
        if self._idle_motion_task is None or self._idle_motion_task.done():
            return False
        self._idle_motion_paused = True
        await asyncio.sleep(settle)
        return True

    async def _idle_motion_loop(self) -> None:
        """Drift the cursor in small random walks while the page lock is free.

        Each iteration: try to acquire the lock non-blocking. If a tool holds
        it, yield immediately and retry after a short sleep (cursor stays where
        the tool left it — correct, since the tool is actively moving it). If
        the lock is free, take one small occasional drift step from the current
        position, release the lock, then sleep ~0.5-1.4s. Rare and small by
        design: at HUMANIZE_LEVEL each dispatch costs ~0.3s and renders a
        short humanized curve, so frequent tiny steps read as nervous
        jitter; a resting hand is still most of the time.
        """
        page = self._page
        if page is None:
            return
        vp = page.viewport_size or {"width": 1280, "height": 800}
        rng = random.Random()
        # Seed the idle position near viewport center if we have no last-known
        # position (e.g. first iteration after recording starts).
        if self._idle_cur_x is None:
            self._idle_cur_x = vp["width"] * 0.5
        if self._idle_cur_y is None:
            self._idle_cur_y = vp["height"] * 0.45

        while not self._stop_idle_motion:
            if self._idle_motion_paused:
                await asyncio.sleep(0.1)
                continue
            try:
                # Never move while a navigation is in flight: page.goto()
                # destroys the document, and a mouse.move mid-teardown errors.
                # safe_goto() sets _navigating for exactly this window.
                if self._navigating:
                    await asyncio.sleep(0.1)
                    continue
                # Non-blocking acquire: if a tool holds the lock, we yield at
                # once so the cursor stays under that tool's control. We do NOT
                # block on the lock — that would serialize behind a slow
                # scroll and defeat the "always slightly moving" goal.
                if self._lock.locked():
                    await asyncio.sleep(0.08)
                    continue
                # Try to grab the lock for just long enough to make one move.
                # If we lose the race (a tool grabbed it between our check and
                # our acquire), the BlockingIOError-equivalent just means try
                # again next iteration.
                acquired = self._lock.acquire_nowait() if hasattr(self._lock, "acquire_nowait") else False
                if not acquired:
                    # Fallback: asyncio.Lock has no acquire_nowait on some
                    # Python versions; emulate with a very short wait_for.
                    try:
                        acquired = await asyncio.wait_for(self._lock.acquire(), timeout=0.01)
                    except asyncio.TimeoutError:
                        await asyncio.sleep(0.08)
                        continue
                try:
                    # One occasional small drift step. Clamped inside the
                    # viewport with a margin so we never drift off-screen or
                    # into the scrollbar. Rare + small by design: at
                    # HUMANIZE_LEVEL each dispatch costs ~0.3s and renders a
                    # short humanized curve, so frequent tiny steps read as
                    # nervous jitter — a human hand rests between moves.
                    step_x = rng.uniform(-14, 14)
                    step_y = rng.uniform(-10, 10)
                    nx = max(40.0, min(vp["width"] - 40.0, self._idle_cur_x + step_x))
                    ny = max(40.0, min(vp["height"] - 40.0, self._idle_cur_y + step_y))
                    from .human_move import mouse_move

                    await mouse_move(page, nx, ny, timeout=5.0)
                    self._idle_cur_x = nx
                    self._idle_cur_y = ny
                    self._last_cursor_pos = (nx, ny)
                finally:
                    if acquired:
                        self._lock.release()
                # Jittered, generous sleep so the drift reads as a resting
                # hand, not a metronome.
                await asyncio.sleep(rng.uniform(0.5, 1.4))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Never let an idle-move failure kill the loop or the recording.
                log_verbose(f"idle motion: non-fatal: {type(exc).__name__}: {exc}")
                await asyncio.sleep(0.2)

    async def set_page(self, page) -> None:
        """Bind the live page once the browser is launched and inject the JS mouse tracker.

        The tracker captures mousemove/mousedown/mouseup at browser rate into a
        buffer. Camoufox's native humanize=True produces ~111 intermediate
        mousemove events per page.mouse.move() — the tracker captures all of
        them. After the session, run.py harvests the buffer and writes
        cursor.json so Cap's export renders a full cursor with spring physics,
        motion blur, and click ripples.

        Camoufox runs add_init_script + page.evaluate in an ISOLATED world by
        default (daijro/camoufox#48, commit c3d5721). mousemove events are
        dispatched to the MAIN world, so an isolated-world listener never fires.
        With main_world_eval=True on the AsyncCamoufox constructor, prefixing an
        evaluate string with "mw:" runs it in the page's main world, where the
        listener actually receives the events Camoufox dispatches.
        """
        self._page = page
        self._last_url = page.url
        log("Injecting JS mouse tracker into MAIN world (mw: prefix)...")
        # add_init_script covers future navigations in the isolated world (best
        # effort fallback), but the mw: evaluate is what actually registers the
        # listener in the main world where mousemove events are dispatched.
        await page.add_init_script(CURSOR_TRACKER_INIT_SCRIPT)
        # Try main-world evaluation with mw: prefix first
        injected_in_main = False
        try:
            await page.evaluate("mw:" + CURSOR_TRACKER_INIT_SCRIPT)
            # Diagnostic: verify the tracker actually landed in main world
            world_check = await page.evaluate("mw:" + """
                () => JSON.stringify({
                    hasTracker: !!window.__cursorTracker,
                    hasHarvestFn: typeof window.__harvestCursorEvents === 'function'
                })
            """)
            log(f"tracker injection (mw:) verified: {world_check}")
            if '"hasTracker":true' in world_check or '"hasTracker": true' in world_check:
                injected_in_main = True
        except Exception as exc:
            log(f"tracker: main-world evaluate failed: {exc}")

        # Fallback: if mw: didn't work, try without prefix (may land in isolated world)
        if not injected_in_main:
            try:
                await page.evaluate(CURSOR_TRACKER_INIT_SCRIPT)
                world_check2 = await page.evaluate("""
                    () => JSON.stringify({
                        hasTracker: !!window.__cursorTracker,
                        world: 'no-prefix'
                    })
                """)
                log(f"tracker injection (no-prefix) verified: {world_check2}")
            except Exception as exc:
                log(f"tracker: fallback evaluate also failed: {exc}")
        self._register_nav_listeners(page)
        log("JS mouse tracker injected into main world. Call start_cursor_tracking() when cap recording begins.")

    def _register_nav_listeners(self, page) -> None:
        """Log every URL transition and recover the tracker after ones we didn't cause.

        Redirects we never initiate — Cloudflare interstitials, HTTP->HTTPS,
        www canonicalization, client-side routing — destroy the document and
        window.__cursorTracker with no call into our code at all. Before this
        handler existed those silently wiped the buffer mid-session with no
        trace in the log. Here we harvest what survived and re-inject.

        safe_goto() sets self._navigating so its own (already handled)
        navigation isn't double-harvested.

        With the TrackerMachine: the listener sends EXTERNAL_NAV to the
        tracker machine. If the machine is in `navigating` (our nav), the
        event is ignored (no matching transition). If in `tracking` (idle),
        it fires -> destroyed -> recovery. The race is structurally
        impossible: the event can't fire from the wrong state.
        """

        def _on_frame_navigated(frame) -> None:
            # Generation guard: listeners stay attached to old tabs after
            # adoption, so ignore events from any page that is no longer the
            # bound one. Without this, a stale tab's late navigation clobbers
            # _last_url and fires tracker recovery on the WRONG (new) tab.
            if page is not self._page:
                return
            # Only the top-level document matters; iframes have their own.
            if frame != page.main_frame:
                return
            new_url = frame.url
            old_url = self._last_url
            self._last_url = new_url
            if old_url == new_url:
                return
            # Check the tracker machine state if available, else fall back
            # to the boolean.
            is_our_nav = self._is_navigating_state() if self._tracker_interp else self._navigating
            tag = "ours" if is_our_nav else "EXTERNAL"
            _kv("capability:nav:framenavigated", scope=tag,
                old=old_url or "-", new=new_url or "-")
            if is_our_nav:
                # safe_goto already harvested and will re-inject after load.
                return
            # A navigation we did not initiate just destroyed the tracker.
            # Send EXTERNAL_NAV to the tracker machine: if in tracking state,
            # it transitions to destroyed -> recovery. If the machine isn't
            # initialized, fall back to the direct recovery call.
            if self._tracker_interp is not None:
                asyncio.ensure_future(self._tracker_send("EXTERNAL_NAV"))
                asyncio.ensure_future(self._recover_after_external_nav(new_url))
            else:
                asyncio.ensure_future(self._recover_after_external_nav(new_url))

        def _on_response(response) -> None:
            if page is not self._page:
                return
            try:
                request = response.request
                if request.resource_type != "document" or request.frame != page.main_frame:
                    return
                _kv("capability:nav:response", status=response.status,
                    url=response.url)
            except Exception:
                pass

        async def _on_dialog(dialog):
            if page is not self._page:
                return
            # A JS alert()/confirm() left open blocks ALL CDP input dispatch:
            # page.mouse.move() hangs until the timeout (an empty 'asyncio.
            # TimeoutError' -> 'error moving cursor: ' with no message). Handle
            # at the source: accept alerts (single button), dismiss the rest
            # (safe defaults for confirm/prompt/beforeunload).
            tag = dialog.type
            try:
                if tag == "alert":
                    await dialog.accept()
                else:
                    await dialog.dismiss()
                log(f"[nav:{self._rel_time()}] auto-dismissed {tag} dialog: {dialog.message[:80]!r}")
            except Exception as exc:
                log(f"[nav] dialog handler failed ({tag}): {exc}")

        try:
            page.on("framenavigated", _on_frame_navigated)
            page.on("response", _on_response)
            page.on("dialog", lambda d: asyncio.ensure_future(_on_dialog(d)))
            log("[nav] navigation listeners registered (framenavigated + document responses + dialogs)")
        except Exception as exc:
            log(f"[nav] could not register navigation listeners (non-fatal): {exc}")

    async def _recover_after_external_nav(self, url: str) -> None:
        """Re-inject and re-start the tracker after a navigation we didn't initiate.

        After successful recovery, sends RECOVER to the tracker machine:
        destroyed -> tracking. After 2 failed attempts, sends RECOVERY_FAILED:
        destroyed -> recoveryFailed (terminal) and sets tracker_verified=False.

        This completes the state-enforced recovery cycle with a bounded retry.
        """
        # Increment recovery attempt counter
        if self._tracker_interp is not None:
            attempts = self._tracker_interp.context.get("recovery_attempts", 0)
            self._tracker_interp.context["recovery_attempts"] = attempts + 1
        else:
            attempts = 0

        try:
            log(f"[nav] recovering tracker after external navigation to {url} "
                f"(attempt {attempts + 1})")
            await self.reinject_tracker()
            await self._restart_tracker_on_new_page()
            moves_lost = len(self._accumulated_moves)
            log(f"[nav] tracker recovered; accumulated buffer holds {moves_lost} moves "
                 f"(events on the destroyed document are unrecoverable)")
            # Send RECOVER: destroyed -> tracking (recovery complete)
            await self._tracker_send("RECOVER")
        except Exception as exc:
            log(f"[nav] tracker recovery failed (attempt {attempts + 1}): {exc}")
            if attempts + 1 >= 2 and self._tracker_interp is not None:
                log("[nav] tracker recovery exhausted after 2 attempts - sending RECOVERY_FAILED")
                await self._tracker_send("RECOVERY_FAILED")
            else:
                log(f"[nav] tracker recovery will retry on next external nav")

    async def start_cursor_tracking(self) -> None:
        """Mark the cursor tracker's start time so all events are relative to recording start.

        The start time is stored in Python so we can re-start tracking on new
        page contexts after navigation while keeping timestamps continuous.
        """
        import time as _time
        self._tracking_start_time = _time.time() * 1000.0  # epoch ms
        page = self.page
        try:
            # Try mw: prefix first, fallback to no-prefix
            started = False
            try:
                await page.evaluate(f"""
                    mw:() => {{
                        if (window.__cursorTracker) {{
                            window.__cursorTracker.start({self._tracking_start_time});
                            return 'injected';
                        }}
                        return null;
                    }}
                """)
                # Verify start() actually set started=true. The old check used
                # `started !== undefined`, which is true when started is false
                # too — so a failed/swallowed start() still reported success
                # and the selftest later looked like a contradiction.
                check = await page.evaluate("mw:" + "() => !!(window.__cursorTracker && window.__cursorTracker.started === true)")
                if check:
                    started = True
            except Exception:
                pass
            if not started:
                await page.evaluate(f"""
                    () => {{
                        if (window.__cursorTracker) {{
                            window.__cursorTracker.start({self._tracking_start_time});
                        }}
                    }}
                """)
                # Verify with the SAME no-prefix path (mw: always throws here).
                check = await page.evaluate("""() => !!(window.__cursorTracker && window.__cursorTracker.started === true)""")
                if check:
                    started = True
            if started:
                _kv("capability:cursor:tracking",
                    status="started",
                    start_time=f"{self._tracking_start_time:.0f}ms")
                # Start the idle-motion loop now so the cursor drifts while the
                # LLM thinks between tool calls. The loop yields to any tool
                # that acquires the page lock, so it never fights scroll/click.
                await self.start_idle_motion()
            else:
                log(f"cursor tracking start failed: tracker not started in main world "
                     f"(window.__cursorTracker.started is not true)")
        except Exception as exc:
            log(f"cursor tracking start failed: {exc}")

    async def _harvest_and_accumulate(self, *, quiet: bool = False) -> None:
        """Harvest the current page's cursor events and merge into the accumulated buffer.

        Called before page.goto() to save events from the document that's about
        to be destroyed. Also called at final harvest to collect the last page's
        events.

        Incremental: the in-page buffer is cleared on every harvest (see
        cursor_tracker_script.py), so repeated calls never double-count.
        quiet=True suppresses the 'mw: evaluate threw' chatter and the empty-buffer
        line for hot-path calls (pre-input harvests) — it still logs whenever
        events were actually captured, and the hard fault (tracker missing in both
        worlds) is always logged.
        """
        page = self.page
        harvested = False
        raw = None
        # JS body shared by both the mw: (main world) and no-prefix (isolated
        # world) evaluate calls. Returns null when __harvestCursorEvents is
        # missing — which distinguishes "wrong world / tracker not injected"
        # from "right world, buffer genuinely empty". Previously the fallback
        # returned an empty {moves:[],...} default in both cases, so a
        # main-world harvest failure silently reported +0 moves and there was
        # no way to tell data loss from an empty buffer.
        harvest_js = """
            () => {
                if (typeof window.__harvestCursorEvents !== 'function') return null;
                return window.__harvestCursorEvents();
            }
        """
        try:
            # Try mw: prefix first — main world is where the tracker lives.
            raw = await page.evaluate("mw:" + harvest_js)
            if raw is not None:
                harvested = True
            else:
                log("cursor tracker harvest: __harvestCursorEvents missing in MAIN world "
                     "(mw:) — tracker not injected there. Trying no-prefix fallback.")
        except Exception as exc:
            if not quiet:
                log(f"cursor tracker harvest: mw: evaluate threw ({type(exc).__name__}: {exc}). "
                     f"Trying no-prefix fallback.")

        # Fallback: try without prefix (lands in the isolated world, where the
        # init script's __harvestCursorEvents may exist but the tracker buffer
        # is always empty because mousemove is never dispatched there).
        if not harvested:
            try:
                raw = await page.evaluate(harvest_js)
            except Exception as exc:
                log(f"cursor tracker harvest failed (both worlds): {exc}")
                return

        if raw is None:
            # Neither world had __harvestCursorEvents. This is a hard fault,
            # not an empty buffer — the tracker was never injected (or was
            # destroyed by an un-harvested navigation). Logging it loudly so a
            # future "0 moves" cursor.json can be attributed correctly instead
            # of looking like the session just had no mouse activity.
            log("cursor tracker harvest: __harvestCursorEvents missing in BOTH worlds — "
                 "tracker not injected on this page. Buffer unchanged.")
            return

        import json
        data = json.loads(raw) if isinstance(raw, str) else raw
        moves = data.get("moves", [])
        clicks = data.get("clicks", [])
        if moves or clicks:
            self._accumulated_moves.extend(moves)
            self._accumulated_clicks.extend(clicks)
        if moves or clicks or not quiet:
            _kv("capability:cursor:harvest", phase="partial",
                moves=len(moves), clicks=len(clicks),
                total_moves=len(self._accumulated_moves),
                total_clicks=len(self._accumulated_clicks))

    async def reinject_tracker(self) -> None:
        """Re-inject the cursor tracker into the main world of the current page.

        Called by run.py right after the INITIAL page.goto(start_url) — the
        page.evaluate("mw:" + ...) done in set_page() ran on about:blank, and
        that document (and its window.__cursorTracker) is destroyed the moment
        the first real navigation happens. add_init_script is supposed to
        re-land the script on the new document automatically, but this is a
        confirmed, currently-open Camoufox race on beta.29
        (daijro/camoufox#738): the init script occasionally lands on a global
        that isn't the one the document ends up with, and the failure is
        swallowed silently inside Camoufox — no exception ever reaches Python.
        This direct, awaited mw: evaluate does not depend on that racy path.

        Safe to call even if tracking hasn't started yet (start_cursor_tracking
        sets the start time separately) — the init script is idempotent.
        """
        page = self.page
        injected = False
        try:
            # Try mw: prefix first
            await page.evaluate("mw:" + CURSOR_TRACKER_INIT_SCRIPT)
            # Verify injection actually landed in MAIN world
            check = await page.evaluate("mw:" + """() => !!window.__cursorTracker""")
            if check:
                log("cursor tracker re-injected into main world after navigation")
                injected = True
        except Exception:
            pass

        # Fallback: try without prefix (proven to work when mw: fails)
        if not injected:
            try:
                await page.evaluate(CURSOR_TRACKER_INIT_SCRIPT)
                check2 = await page.evaluate("""
                    () => !!window.__cursorTracker
                """)
                if check2:
                    log("cursor tracker re-injected (fallback, no-prefix)")
                else:
                    log("cursor tracker re-injection failed: tracker still not present")
            except Exception as exc:
                log(f"cursor tracker re-injection failed: {exc}")

    async def _restart_tracker_on_new_page(self) -> None:
        """Re-inject and re-start the cursor tracker on a new page context.

        After page.goto(), the old document (and its window.__cursorTracker) is
        destroyed. add_init_script re-injects the script automatically, but in
        the isolated world — not the main world where mousemove events are
        dispatched. We re-inject via evaluate and re-start with the
        ORIGINAL start time so all timestamps remain continuous.
        """
        page = self.page
        # NOTE: this Camoufox build runs plain (no-prefix) evaluate in the MAIN
        # world ('mw:' prefix is disabled and always throws). Evidence: the
        # no-prefix selftest probe sees window.__cursorTracker AND its live move
        # count, and the no-prefix harvest fallback returns real events. So the
        # no-prefix path IS the main-world path here; 'mw:' checks must not be
        # treated as the source of truth (they false-negative).
        try:
            if self._tracking_start_time is not None:
                # Re-inject (idempotent) plain, then re-start with the ORIGINAL
                # start time so timestamps stay continuous across pages.
                injected = False
                try:
                    await page.evaluate(CURSOR_TRACKER_INIT_SCRIPT)
                    check = await page.evaluate("""() => !!window.__cursorTracker""")
                    if check:
                        injected = True
                    else:
                        # add_init_script race: tracker missing on new document.
                        log("cursor tracker re-injection: not present after plain injection")
                except Exception:
                    pass
                if injected:
                    started_check = await page.evaluate(
                        f"() => {{ if (window.__cursorTracker) {{ window.__cursorTracker.start({int(self._tracking_start_time)}); return window.__cursorTracker.started === true; }} return false; }}"
                    )
                    if started_check:
                        log(f"cursor tracker re-started on new page (startTime={self._tracking_start_time:.0f}ms, verified)")
                    else:
                        log("cursor tracker re-start: start() did not report started=true (non-fatal)")
                else:
                    log("cursor tracker re-injection failed: tracker not present on new page")
        except Exception as exc:
            log(f"cursor tracker re-start on new page failed (non-fatal): {exc}")

    async def cursor_selftest(self) -> bool:
        """Prove the main-world listener actually receives dispatched mouse events.

        Two independent faults can both produce an empty cursor.json and the
        final harvest log cannot tell them apart:
          (a) the buffer was collected but destroyed by an un-harvested
              navigation, or
          (b) the main-world listener never fired at all (Camoufox
              isolated-world injection landing on the wrong global).

        This check collapses that ambiguity before recording starts: read the
        tracker's move count, issue real page.mouse.move() calls, then re-read.
        A non-zero delta proves (b) is not happening — injection is live and
        Camoufox's dispatched events reach our listener. A zero delta proves it
        is, and points at the Camoufox build rather than our harvest ordering.

        Returns True if events were observed. Never raises — a broken self-test
        must not abort a recording run.
        """
        page = self.page
        probe_mw = """
            mw:() => {
                if (!window.__cursorTracker) return null;
                return JSON.stringify({
                    present: true,
                    started: window.__cursorTracker.started,
                    moves: window.__cursorTracker.moves.length,
                    harvestFn: typeof window.__harvestCursorEvents,
                });
            }
        """
        probe_plain = """
            () => {
                if (!window.__cursorTracker) return null;
                return JSON.stringify({
                    present: true,
                    started: window.__cursorTracker.started,
                    moves: window.__cursorTracker.moves.length,
                    harvestFn: typeof window.__harvestCursorEvents,
                });
            }
        """
        try:
            import json as _json

            # Try mw: first, fallback to plain evaluate
            raw_before = None
            try:
                raw_before = await page.evaluate(probe_mw)
            except Exception:
                pass
            if not raw_before:
                raw_before = await page.evaluate(probe_plain)

            before = _json.loads(raw_before) if isinstance(raw_before, str) else raw_before
            _kv("capability:cursor:selftest", phase="probe",
                present=before.get("present"), started=before.get("started"),
                harvest_fn=before.get("harvestFn"), moves=before.get("moves"))
            if not before.get("present"):
                _kv("capability:cursor:selftest", status="FAIL",
                    reason="tracker-missing")
                return False

            vp = page.viewport_size or {"width": 1280, "height": 800}
            # page.mouse.move() with humanize can hang for minutes when the
            # Camoufox build's bezier-curve generator is broken
            # ("humanize:maxTime is not a double"). Wrap with a hard timeout
            # so the self-test can't stall the entire recording session.
            try:
                from .human_move import mouse_move

                await mouse_move(page, vp["width"] * 0.4,
                                 vp["height"] * 0.4, timeout=5.0)
                await mouse_move(page, vp["width"] * 0.6,
                                 vp["height"] * 0.55, timeout=5.0)
                # keep the model's "where is the cursor" belief in sync so the
                # next human_move starts from the real current position
                self._last_cursor_pos = (vp["width"] * 0.6, vp["height"] * 0.55)
                self._idle_cur_x = vp["width"] * 0.6
                self._idle_cur_y = vp["height"] * 0.55
            except asyncio.TimeoutError:
                _kv("capability:cursor:selftest", status="TIMEOUT",
                    reason="mouse-move>5s")
                return False

            raw_after = None
            try:
                raw_after = await asyncio.wait_for(
                    page.evaluate(probe_mw), timeout=3.0
                )
            except (Exception, asyncio.TimeoutError):
                pass
            if not raw_after:
                try:
                    raw_after = await asyncio.wait_for(
                        page.evaluate(probe_plain), timeout=3.0
                    )
                except (Exception, asyncio.TimeoutError):
                    pass
            after = _json.loads(raw_after) if isinstance(raw_after, str) else raw_after
            delta = int(after.get("moves", 0)) - int(before.get("moves", 0))
            if delta > 0:
                _kv("capability:cursor:selftest", status="PASS",
                    moves=delta, probes=2, total=after.get("moves"))
                return True
            _kv("capability:cursor:selftest", status="FAIL",
                reason="listener-mismatch", started=after.get("started"))
            return False
        except Exception as exc:
            _kv("capability:cursor:selftest", status="ERROR",
                error=type(exc).__name__)
            return False

    async def safe_goto(self, url: str, *, timeout: int = 20000) -> None:
        """Navigate without losing the cursor buffer.

        page.goto() destroys the current document along with
        window.__cursorTracker and every event it holds. This is the ONLY
        sanctioned way to navigate: it harvests the outgoing document's events
        into the accumulated buffer first, then re-injects and re-starts the
        tracker on the new document with the original start time so timestamps
        stay continuous.

        Every navigation call site must route through here. A bare page.goto()
        anywhere in the session silently discards the whole session's cursor
        data, which is exactly the bug this method exists to make impossible.
        """
        page = self.page
        before = len(self._accumulated_moves)
        await self._harvest_and_accumulate()
        _kv("capability:nav:goto", url=url, buffered_moves=len(
            self._accumulated_moves))
        # Send GOTO to the tracker machine: tracking -> navigating.
        # While in navigating, any EXTERNAL_NAV event from the framenavigated
        # listener is ignored (our nav, already handled). This replaces the
        # _navigating boolean with a state-enforced invariant.
        await self._ensure_tracker_machine()
        await self._tracker_send("GOTO")
        self._navigating = True  # backward compat for code that reads the boolean
        try:
            await page.goto(url, wait_until="load", timeout=timeout)
        finally:
            self._navigating = False  # backward compat
            self._last_url = page.url
        # Send NAV_DONE: navigating -> tracking. The navigating.exit action
        # logs the tracker restart; the actual restart happens below.
        await self._tracker_send("NAV_DONE")
        _kv("capability:nav:landed", url=page.url)
        await self._restart_tracker_on_new_page()

    async def harvest_cursor_events(self) -> dict[str, Any]:
        """Harvest all cursor events accumulated across page navigations.

        Called by run.py after the agent session. Merges the current page's
        events with the accumulated buffer from previous page contexts.

        Returns {moves: [...], clicks: [...], started: bool} with clientX/Y
        coords. run.py normalizes them to screen space (0.0-1.0) and writes
        cursor.json.
        """
        # Stop the idle-motion loop before harvesting so it doesn't inject a
        # stray move mid-harvest or fight the harvest's page.evaluate.
        await self.stop_idle_motion()
        # Harvest the current (last) page's events and merge into the buffer
        await self._harvest_and_accumulate()

        moves = self._accumulated_moves
        clicks = self._accumulated_clicks
        started = self._tracking_start_time is not None
        _kv("capability:cursor:harvest", phase="final",
            moves=len(moves), clicks=len(clicks), started=started)
        return {
            "moves": list(moves),
            "clicks": list(clicks),
            "started": started,
        }
