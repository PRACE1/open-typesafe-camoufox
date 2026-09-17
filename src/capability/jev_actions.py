"""
JevCapability: 2-tool mixin (read_frame + move_cursor) on top of CamoufoxCapability.

- read_frame: screenshot JPEG at 60fps capture, caller decimates to 2-5fps
  keyframes; converts to BRAILLE TEXT (jev_ascii, math ported from
  prime-agent scripts/render-logo.py --style braille) and sends the text to
  the Jev decider (the `jev read`). Mirrors prime-agent's attach-image
  fallback contract: vision-capable path loads pixels; non-vision path
  routes to a text rendering with an explicit guard message instead of
  erroring — but here the harness owns the rendering deterministically
  instead of relying on the model to improvise PIL code in the kernel.
- move_cursor: normalized x/y (long/lat 0-1) -> pixels -> humanized move
  (human_move: a lateral mid-point for long moves, humanize bezier for the
  rest; HUMANIZE_LEVEL speeds each dispatch to ~0.3s), optional click/type/
  key.
  ELEMENT MODE (action.element=<idx>): the video-agent pattern —
  scroll-into-view -> model-based highlight (a single clean 5-hop loop via
  _hover_and_human_highlight; the first hop doubles as the approach and the
  click's own humanized move settles the center; geometric octagon fallback)
  -> click focus -> type -> key. The idx comes from find_elements().

find_elements() probes the live DOM for actionable elements (inputs,
buttons, links) in reading order, tags each with [data-jev="<idx>"], and
returns the element map the planner targets.

Mixed into JevCapability(CamoufoxCapability) so tracker lifecycle, safe_goto,
idle-motion, and cursor.json harvest from cursor_tracking.py are reused verbatim.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from pydantic_ai import RunContext

from jev_ascii import frame_to_braille
from jev_client import decide_cursor
from jev_tools import MoveCursorAction, ReadFrameAction
from src.capability.camoufox_capability import CamoufoxCapability
from src.capability.deps import CamoufoxDeps
from src.capability.element_probe import ELEMENT_PROBE_JS
from src.capability.human_move import human_move as _human_move
from src.capability.logging_utils import log
from typing import Any

# Mirrors attach-image's guard wording so transcripts stay legible:
# vision path loads pixels, non-vision path routes to text rendering.
NO_VISION_GUARD = (
    "decider has no vision capability; routing frame through text rendering (braille)"
)


def _mask(text: str | None) -> str:
    """Never log secret values — show length only."""
    if text is None:
        return "none"
    return f"<{len(text)} chars masked>"


def _exc_str(exc: BaseException) -> str:
    """Non-empty reason even for message-less exceptions (asyncio.TimeoutError)."""
    return str(exc) or type(exc).__name__


def _resolve_placeholders(text: str) -> str:
    """Substitute {ENV_NAME} placeholders from process env at runtime."""
    import re

    def repl(m) -> str:
        return os.environ.get(m.group(1), m.group(0))

    return re.sub(r"\{([A-Z_][A-Z0-9_]*)\}", repl, text)


class JevCapability(CamoufoxCapability):
    async def find_elements(self) -> list[dict[str, Any]]:
        """Probe the current page for actionable elements, reading order.

        Returns [{"idx","sel","kind","type","id","label","placeholder","text","cx","cy"}, ...]
        cx/cy are viewport-normalized (0-1) centers for the planner.
        """
        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    self.page.evaluate(ELEMENT_PROBE_JS), timeout=10.0
                )
                return result or []
            except Exception as exc:
                log(f"find_elements: {_exc_str(exc)}")
                return []

    async def read_frame(
        self, ctx: RunContext[CamoufoxDeps], action: ReadFrameAction
    ) -> str:
        """Capture still, render to braille text, ask Jev decider, stash x/y."""
        async with self._lock:
            page = self.page
            try:
                shot = await page.screenshot(type="jpeg", quality=70)
            except Exception as exc:
                msg = f"error capturing frame: {_exc_str(exc)}"
                log(msg)
                return msg
            try:
                from PIL import Image

                img = Image.open(io.BytesIO(shot)).convert("RGB")
                w, h = img.size
                max_w = max(320, min(1920, action.max_width))
                if w > max_w:
                    img = img.resize((max_w, int(h * max_w / w)), Image.LANCZOS)
                frame_text, cols, rows = frame_to_braille(
                    img, width_cols=action.width_cols, threshold=action.threshold
                )
            except Exception as exc:
                msg = f"error rendering frame to text: {_exc_str(exc)}"
                log(msg)
                return msg
            if not frame_text.strip():
                msg = "frame blank after text rendering; skipping Jev call"
                log(msg)
                return msg
            log(NO_VISION_GUARD)
            prompt = (
                getattr(ctx.deps, "log", [""])[-1]
                if getattr(ctx.deps, "log", None)
                else "Move to most actionable element."
            )
            try:
                decision = await decide_cursor(
                    frame_text, prompt=str(prompt), cols=cols, rows=rows
                )
            except Exception as exc:
                msg = f"jev decide failed: {_exc_str(exc)}"
                log(msg)
                return msg
            # Stash for the planner + next move_cursor call.
            ctx.deps.browser_urls["__jev_last__"] = (
                f"{decision.x:.4f},{decision.y:.4f},{decision.confidence:.3f}"
            )
            ctx.deps.browser_urls["__jev_frame__"] = frame_text
            ctx.deps.browser_urls["__jev_grid__"] = f"{cols}x{rows}"
            msg = (
                f"frame ({cols}x{rows} braille) -> jev x={decision.x:.3f} y={decision.y:.3f} "
                f"click={1 if decision.click else 0} conf={decision.confidence:.2f}"
            )
            log(msg)
            return msg

    async def get_page_text(self, limit: int = 1500) -> str:
        """Visible body text — the planner's ground truth for reading results
        (e.g. 'SENT: Jev,braille'), which low-contrast pages can't show in braille."""
        async with self._lock:
            try:
                text = await asyncio.wait_for(
                    self.page.evaluate("() => document.body ? document.body.innerText : ''"),
                    timeout=10.0,
                )
                return (text or "")[:limit]
            except Exception as exc:
                log(f"get_page_text: {_exc_str(exc)}")
                return ""

    async def _clear_field(self, page) -> None:
        """Select-all + delete so type_text replaces instead of appending.

        Without this, typing into an already-filled input (e.g. Google's
        search box on a results page) concatenates onto the old query and
        the query grows every step. Fail-soft: if the keypress fails the
        type still proceeds.
        """
        try:
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.press("Backspace")
        except Exception as exc:  # noqa: BLE001
            log(f"clear field: {_exc_str(exc)}")

    async def move_cursor(
        self, ctx: RunContext[CamoufoxDeps], action: MoveCursorAction
    ) -> str:
        """Humanized cursor action. humanize forced on (HUMANIZE_LEVEL-scaled).

        Element mode (action.element set): video-agent pattern —
        scroll-into-view -> clean model loop highlight (first hop doubles as
        the curved approach; the click's own humanized move settles the
        center; octagon fallback) -> click focus -> type -> key.
        Pixel mode: model move -> click (focus) -> type -> key.
        Typed values resolve {ENV_NAME} placeholders at runtime and are NEVER logged.
        """
        async with self._lock:
            paused = await self.quiesce_idle_motion()
            try:
                page = self.page
                vp = page.viewport_size or {"width": 1280, "height": 800}
                try:
                    if action.element is not None:
                        return await self._move_cursor_element(page, vp, action)
                    px = int(action.x * vp["width"])
                    py = int(action.y * vp["height"])
                    # model-curve approach from where the cursor actually is,
                    # with a plain move as safety net (e.g. model unavailable)
                    start = self._last_cursor_pos or (10.0, 10.0)
                    used_model = await _human_move(
                        page, start, (px, py), vp=vp
                    )
                    if not used_model:
                        await asyncio.wait_for(page.mouse.move(px, py), timeout=10.0)
                    self._last_cursor_pos = (float(px), float(py))
                    parts = [
                        f"moved to ({action.x:.3f},{action.y:.3f}) "
                        f"model={'on' if used_model else 'off'}"
                    ]
                    if action.click or action.type_text is not None:
                        await self._harvest_and_accumulate(quiet=True)
                        await asyncio.wait_for(page.mouse.click(px, py), timeout=10.0)
                        parts.append("click")
                    if action.type_text is not None:
                        secret = _resolve_placeholders(action.type_text)
                        await self._harvest_and_accumulate(quiet=True)
                        await self._clear_field(page)
                        await page.keyboard.type(secret, delay=15)
                        parts.append(f"typed {_mask(secret)}")
                    if action.key is not None:
                        await self._harvest_and_accumulate(quiet=True)
                        await page.keyboard.press(action.key)
                        parts.append(f"key={action.key}")
                    msg = " + ".join(parts)
                    log(msg)
                    return msg
                except Exception as exc:
                    msg = f"error moving cursor: {_exc_str(exc)}"
                    log(msg)
                    return msg
            finally:
                if paused:
                    self.resume_idle_motion()

    async def _move_cursor_element(
        self, page, vp: dict[str, int], action: MoveCursorAction
    ) -> str:
        """video-agent element pattern: scroll -> circle -> click -> type -> key."""
        sel = f'[data-jev="{action.element}"]'
        locator = page.locator(sel).first
        await asyncio.wait_for(locator.scroll_into_view_if_needed(timeout=5000), timeout=15.0)
        box = await locator.bounding_box()
        if not box:
            msg = f"error: element #{action.element} has no bounding box (hidden?)"
            log(msg)
            return msg
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await self._hover_and_highlight(box)
        px = min(max(int(cx), 0), int(vp["width"]) - 1)
        py = min(max(int(cy), 0), int(vp["height"]) - 1)
        # Drains the in-page buffer (approach + loop) into the Python
        # accumulator BEFORE any dispatch that could submit/navigate — a form
        # submit destroys the document, and only what's already accumulated
        # survives it.
        await self._harvest_and_accumulate(quiet=True)
        # The click's own humanized move is the settle onto the center — no
        # separate move dispatch needed (halves the tail cost).
        await asyncio.wait_for(page.mouse.click(px, py), timeout=10.0)
        parts = [
            f"element #{action.element} scroll+circle+click at "
            f"({px / vp['width']:.3f},{py / vp['height']:.3f}) humanize=true"
        ]
        if action.type_text is not None:
            secret = _resolve_placeholders(action.type_text)
            await self._harvest_and_accumulate(quiet=True)
            await self._clear_field(page)
            await page.keyboard.type(secret, delay=15)
            parts.append(f"typed {_mask(secret)}")
        if action.key is not None:
            await self._harvest_and_accumulate(quiet=True)
            await page.keyboard.press(action.key)
            parts.append(f"key={action.key}")
        msg = " + ".join(parts)
        log(msg)
        return msg