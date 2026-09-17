"""
The JS mouse tracker injected via page.add_init_script / page.evaluate.

Runs BEFORE any page scripts. Captures mousemove/mousedown/mouseup at
browser rate into a buffer that cursor_tracking.py harvests after (or during)
the agent session to synthesize Cap's cursor.json.

Coordinates are stored as clientX/clientY (viewport-relative, CSS pixels).
They're normalized to screen space (0.0-1.0) later, during harvest, since
Cap's cursor.json expects coords normalized to the recorded screen.
"""

from __future__ import annotations

# JS mouse tracker injected via page.add_init_script. Runs BEFORE any page
# scripts. Captures mousemove/mousedown/mouseup at browser rate into a buffer
# that we harvest after the agent session to synthesize Cap's cursor.json.
#
# Coordinates are stored as clientX/clientY (viewport-relative, in CSS pixels).
# We normalize them to screen space (0.0-1.0) during harvest, since Cap's
# cursor.json expects normalized coords relative to the recorded screen.
CURSOR_TRACKER_INIT_SCRIPT = r"""
(() => {
    if (window.__cursorTracker) return;
    window.__cursorTracker = { moves: [], clicks: [], startTime: null, started: false };

    window.__cursorTracker.start = function(t) {
        window.__cursorTracker.startTime = t || (typeof performance !== 'undefined' ? performance.timeOrigin + performance.now() : Date.now());
        window.__cursorTracker.started = true;
    };

    function ts() {
        if (!window.__cursorTracker.started || window.__cursorTracker.startTime == null) return null;
        var now = (typeof performance !== 'undefined' ? performance.timeOrigin + performance.now() : Date.now());
        return now - window.__cursorTracker.startTime;
    }

    window.addEventListener('mousemove', function(e) {
        var t = window.__cursorTracker;
        var time = ts();
        if (time == null) return;
        t.moves.push({ x: e.clientX, y: e.clientY, time: time });
    }, { passive: true });

    window.addEventListener('mousedown', function(e) {
        var t = window.__cursorTracker;
        var time = ts();
        if (time == null) return;
        t.clicks.push({ x: e.clientX, y: e.clientY, time: time, down: true, button: e.button });
    }, { passive: true });

    window.addEventListener('mouseup', function(e) {
        var t = window.__cursorTracker;
        var time = ts();
        if (time == null) return;
        t.clicks.push({ x: e.clientX, y: e.clientY, time: time, down: false, button: e.button });
    }, { passive: true });

    // Expose a harvest function so run.py can call page.evaluate(harvest_script).
    // DESTRUCTIVE: clears the in-page buffer after each read so every harvest is
    // incremental — extending the Python accumulator can never double-count, and
    // a navigation that destroys this document can only drop events recorded
    // after the last harvest, never an entire gesture.
    window.__harvestCursorEvents = function() {
        var t = window.__cursorTracker;
        var out = { moves: t.moves, clicks: t.clicks, started: t.started };
        t.moves = [];
        t.clicks = [];
        return JSON.stringify(out);
    };
})();
"""
