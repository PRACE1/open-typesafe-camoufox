"""
actions.py — one deterministic handler per action kind (typesafe's actions.py).

Each handler drives the platform adapter and returns a history line.
Free text arrives already composed (writer.py) or as an {ENV} placeholder
resolved here at execution — values never enter prompts; logs carry
length-masked placeholders only.
"""

from __future__ import annotations

import asyncio

from .capability.logging_utils import log
from .deps import ElementRef
from .writer import _mask, _resolve_placeholders


async def _clear_field(platform) -> None:
    """Select-all + delete so type_at replaces instead of appending.

    Fail-soft: if the keypress fails the type still proceeds.
    """
    try:
        await platform.page.keyboard.press("ControlOrMeta+A")
        await platform.page.keyboard.press("Backspace")
    except Exception as exc:  # noqa: BLE001
        log(f"clear field: {exc}")


async def _element_center(platform, elements: list[ElementRef], idx: int):
    """Scroll element idx into view; return (px, py, box) or None."""
    page = platform.page
    sel = f'[data-jev="{idx}"]'
    locator = page.locator(sel).first
    await asyncio.wait_for(locator.scroll_into_view_if_needed(timeout=5000), timeout=15.0)
    box = await locator.bounding_box()
    if not box:
        return None
    vp = page.viewport_size or {"width": 1280, "height": 800}
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    px = min(max(int(cx), 0), int(vp["width"]) - 1)
    py = min(max(int(cy), 0), int(vp["height"]) - 1)
    return px, py, box


async def click_item(platform, elements: list[ElementRef], idx: int) -> str:
    """Scroll + human-loop highlight + click on element idx."""
    async with platform._lock:
        paused = await platform.quiesce_idle_motion()
        try:
            page = platform.page
            vp = page.viewport_size or {"width": 1280, "height": 800}
            found = await _element_center(platform, elements, idx)
            if not found:
                msg = f"error: element #{idx} has no bounding box (hidden?)"
                log(msg)
                return msg
            px, py, box = found
            # Drain the in-page buffer BEFORE any dispatch that could
            # submit/navigate — a form submit destroys the document, and only
            # what's already accumulated survives it.
            await platform._harvest_and_accumulate(quiet=True)
            await platform._hover_and_highlight(box)
            # The click's own humanized move is the settle onto the center.
            await asyncio.wait_for(page.mouse.click(px, py), timeout=10.0)
            msg = (
                f"element #{idx} scroll+circle+click at "
                f"({px / vp['width']:.3f},{py / vp['height']:.3f}) humanize=true"
            )
            log(msg)
            return msg
        except Exception as exc:  # noqa: BLE001
            msg = f"error clicking element #{idx}: {exc}"
            log(msg)
            return msg
        finally:
            if paused:
                platform.resume_idle_motion()


async def type_at(platform, elements: list[ElementRef], idx: int, text: str) -> str:
    """Focus element idx, clear it, then type the composed text."""
    async with platform._lock:
        paused = await platform.quiesce_idle_motion()
        try:
            page = platform.page
            vp = page.viewport_size or {"width": 1280, "height": 800}
            found = await _element_center(platform, elements, idx)
            if not found:
                msg = f"error: element #{idx} has no bounding box (hidden?)"
                log(msg)
                return msg
            px, py, box = found
            await platform._harvest_and_accumulate(quiet=True)
            await platform._hover_and_highlight(box)
            await asyncio.wait_for(page.mouse.click(px, py), timeout=10.0)
            secret = _resolve_placeholders(text)
            await platform._harvest_and_accumulate(quiet=True)
            await _clear_field(platform)
            await page.keyboard.type(secret, delay=15)
            msg = (
                f"element #{idx} scroll+circle+click at "
                f"({px / vp['width']:.3f},{py / vp['height']:.3f}) humanize=true"
                f" + typed {_mask(secret)}"
            )
            log(msg)
            return msg
        except Exception as exc:  # noqa: BLE001
            msg = f"error typing at element #{idx}: {exc}"
            log(msg)
            return msg
        finally:
            if paused:
                platform.resume_idle_motion()


async def press_key(platform, key: str) -> str:
    """Press Enter/Tab/Escape after typing (e.g. Enter to submit)."""
    async with platform._lock:
        paused = await platform.quiesce_idle_motion()
        try:
            await platform._harvest_and_accumulate(quiet=True)
            await platform.page.keyboard.press(key)
            msg = f"key={key}"
            log(msg)
            return msg
        except Exception as exc:  # noqa: BLE001
            msg = f"error pressing {key}: {exc}"
            log(msg)
            return msg
        finally:
            if paused:
                platform.resume_idle_motion()


async def goto_url(platform, url: str) -> str:
    """Navigate via safe_goto (harvests cursor buffer across documents)."""
    try:
        await platform.safe_goto(url)
        msg = f"navigated to {platform._last_url}"
        log(msg)
        return msg
    except Exception as exc:  # noqa: BLE001
        msg = f"nav failed: {exc}"
        log(msg)
        return msg


def done_note_valid(note: str) -> bool:
    """done is only accepted with a non-empty outcome note."""
    return bool((note or "").strip())


__all__ = ["click_item", "type_at", "press_key", "goto_url", "done_note_valid"]
