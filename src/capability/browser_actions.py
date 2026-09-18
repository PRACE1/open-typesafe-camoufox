"""
BrowserActionsMixin: cursor-highlight verbs kept after the typesafe refactor.

Only _hover_and_highlight survives here. The legacy video-agent verbs
(scroll/click/navigate/paginate/discover_page_elements and the
generated-site whitelist) were deleted along with tool_schemas.py — the
solver registers exactly two tools and element scroll+click+type lives in
actions.py. This mixin only renders the human highlight ring around a live
bounding box, using Vinyzu/cursory recorded-human trajectories instead of
our old synthetic ellipse/octagon math.

Mixed into browser.CamoufoxPlatform alongside CursorTrackingMixin.
"""

from __future__ import annotations

from .logging_utils import log


class BrowserActionsMixin:
    async def _hover_and_highlight(self, box: dict[str, float]) -> None:
        """Move cursor over an element with human-like motion, then highlight it.

        One cursory-rendered revolution (recorded-human arcs between 5
        ellipse waypoints): the first dispatch doubles as the curved
        approach from wherever the cursor is, and the following click's
        own humanized move closes the gap and settles on the center — no
        extra dispatches. Fails soft: any cursory/replay error falls back
        to one direct cursory move onto the center so the highlight is
        never silently skipped.
        """
        page = self.page
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        vp = page.viewport_size or {"width": 1280, "height": 800}
        radius_x = max(35.0, min(100.0, box["width"] * 0.4))
        radius_y = max(25.0, min(60.0, box["height"] * 0.4))
        try:
            from .human_move import cursory_loop, cursory_move

            start = self._last_cursor_pos or (vp["width"] / 2, vp["height"] / 2)
            approach_px = ((cx - start[0]) ** 2 + (cy - start[1]) ** 2) ** 0.5
            loop_start = await cursory_loop(
                page, cx, cy, radius_x, radius_y, hops=5, vp=vp)
            if loop_start is not None:
                self._last_cursor_pos = (cx, cy)
                log(
                    f"highlight: cursory ring from {approach_px:.0f}px away "
                    f"(rx={radius_x:.0f}, ry={radius_y:.0f}); click settles on center"
                )
            else:
                log("highlight: cursory ring failed — settling directly")
                if await cursory_move(page, start, (cx, cy), vp=vp):
                    self._last_cursor_pos = (cx, cy)
                    log("highlight: cursory settle done")
        except Exception as exc:
            log(f"highlight: warning: {exc}")
