"""
BrowserActionsMixin: cursor-highlight verbs kept after the typesafe refactor.

Only _hover_and_highlight + _octagon_highlight survive here. The legacy
video-agent verbs (scroll/click/navigate/paginate/discover_page_elements and
the generated-site whitelist) were deleted along with tool_schemas.py — the
solver registers exactly two tools and element scroll+click+type lives in
actions.py. This mixin only renders the human highlight ring around a live
bounding box.

Mixed into browser.CamoufoxPlatform alongside CursorTrackingMixin.
"""

from __future__ import annotations

import asyncio
import math
import random

from .logging_utils import log


class BrowserActionsMixin:
    async def _hover_and_highlight(self, box: dict[str, float]) -> None:
        """Move cursor over an element with human-like motion, then highlight it.

        One clean model-driven revolution (5 hops on a plain ellipse):
        the first dispatch doubles as the curved approach from wherever
        the cursor is (the browser's per-dispatch humanize bezier renders
        each arc), and the following click's own humanized move closes
        the gap and settles on the center — no extra dispatches. Fails
        soft: any model/replay error falls back to the geometric octagon
        so the highlight is never silently skipped.
        """
        page = self.page
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        vp = page.viewport_size or {"width": 1280, "height": 800}
        radius_x = max(35.0, min(100.0, box["width"] * 0.4))
        radius_y = max(25.0, min(60.0, box["height"] * 0.4))
        try:
            from .human_move import human_loop

            start = self._last_cursor_pos or (vp["width"] / 2, vp["height"] / 2)
            approach_px = ((cx - start[0]) ** 2 + (cy - start[1]) ** 2) ** 0.5
            loop_start = await human_loop(page, cx, cy, radius_x, radius_y, hops=5, vp=vp)
            if loop_start is not None:
                self._last_cursor_pos = (cx, cy)
                log(
                    f"highlight: human loop from {approach_px:.0f}px away "
                    f"(rx={radius_x:.0f}, ry={radius_y:.0f}); click settles on center"
                )
            else:
                log("highlight: human-loop failed — falling back to octagon sweep")
                await self._octagon_highlight(page, cx, cy, radius_x, radius_y)
        except Exception as exc:
            log(f"highlight: warning: {exc}")

    async def _octagon_highlight(
        self,
        page,
        cx: float,
        cy: float,
        radius_x: float,
        radius_y: float,
    ) -> None:
        """Legacy geometric 8-sweep highlight (fallback only)."""
        try:
            await asyncio.wait_for(page.mouse.move(cx, cy), timeout=5.0)
            await asyncio.sleep(0.015)
            for i in range(8):
                angle = (2 * math.pi * i) / 8
                jitter_r = random.uniform(0.9, 1.1)
                ox = cx + math.cos(angle) * radius_x * jitter_r
                oy = cy + math.sin(angle) * radius_y * jitter_r
                await asyncio.wait_for(page.mouse.move(ox, oy), timeout=5.0)
                await asyncio.sleep(0.006)
            await asyncio.wait_for(page.mouse.move(cx, cy), timeout=5.0)
            await asyncio.sleep(0.015)
            log("highlight: octagon fallback done")
            self._last_cursor_pos = (cx, cy)
        except asyncio.TimeoutError:
            log("highlight: octagon fallback TIMEOUT (mouse.move >5s)")
        except Exception as exc:
            log(f"highlight: octagon fallback warning: {exc}")
