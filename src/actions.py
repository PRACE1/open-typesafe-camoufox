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
from .perception import find_elements, get_page_text
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


POINT_CHECK_JS = """({sel, x, y}) => {
  const target = document.querySelector(sel);
  if (!target) return 'gone';
  const el = document.elementFromPoint(x, y);
  if (!el) return 'void';
  if (target === el || target.contains(el)) return 'hit';
  const cls = (el.getAttribute && el.getAttribute('class')) || '';
  return 'covered:' + (el.tagName || '?').toLowerCase()
    + (cls ? '.' + String(cls).replace(/\\s+/g, ' ').slice(0, 40) : '');
}"""


def action_failed(msg: str | None) -> bool:
    """True when a handler reports failure (a no-op, not a move).

    All failure messages start with "error: " by convention so the runner
    can account them without parsing prose.
    """
    return bool(msg) and msg.startswith("error")


_CLICK_THROUGH_TAGS = ("a", "button")


def _covering_tag(verdict: str) -> str:
    """Tag of the element covering the click point ('' unless covered)."""
    if not verdict.startswith("covered:"):
        return ""
    return verdict[len("covered:"):].split(".")[0].strip().lower()


def may_click_through(verdict: str) -> bool:
    """True when the covering element is itself clickable.

    A human clicking the target's center hits that anchor/button too, so
    dispatching is faithful — something navigates instead of nothing. Blanket
    overlays (div/span cookie walls) still skip.
    """
    return _covering_tag(verdict) in _CLICK_THROUGH_TAGS


async def _point_status(page, sel: str, px: int, py: int) -> str:
    """Ask the DOM what is actually under the click point."""
    try:
        res = await asyncio.wait_for(
            page.evaluate(POINT_CHECK_JS, {"sel": sel, "x": px, "y": py}),
            timeout=10.0,
        )
        return str(res or "void")
    except Exception as exc:  # noqa: BLE001
        return f"error: point check failed: {exc}"


async def _refresh_snapshot(platform) -> str:
    """Fresh DOM snapshot right after a click; returns a one-line digest.

    Clicks often expand content in place (accordions, snippet expansion,
    "people also ask") instead of navigating. The next step re-perceives
    anyway, but rebuilding context HERE lets the result carry what actually
    changed. Reads the CURRENT tab (post-adopt). Fail-soft: '' when the
    document is gone (navigation still in flight).
    """
    try:
        elements = await find_elements(platform)
    except Exception:  # noqa: BLE001
        elements = []
    try:
        text = await get_page_text(platform, limit=300)
    except Exception:  # noqa: BLE001
        text = ""
    if not elements and not (text or "").strip():
        return ""
    head = (text or "").strip().replace("\n", " ")[:120]
    digest = f"fresh({len(elements)} elements, {len((text or '').strip())}ch"
    if head:
        digest += f" :: {head}"
    return digest + ")"


async def _verified_center(platform, elements: list[ElementRef], idx: int):
    """Fresh box + point check with one re-scroll retry.

    The highlight takes ~2s; lazy rendering shifts layout and overlays
    intercept in that window. Returns (px, py, verdict) where verdict is
    'hit', 'gone', 'void', or 'covered:<tag.class>'.
    """
    page = platform.page
    sel = f'[data-jev="{idx}"]'
    for attempt in (1, 2):
        found = await _element_center(platform, elements, idx)
        if not found:
            return None, None, "gone"
        px, py, _box = found
        status = await _point_status(page, sel, px, py)
        if status == "hit" or attempt == 2 or not status.startswith("covered"):
            return px, py, status
    return None, None, "gone"  # unreachable


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
            # Re-measure AFTER the highlight and prove the point hits the
            # target: layout shifts and overlays in between mean a blind
            # click dispatches yet activates nothing. A covering anchor or
            # button still gets the click (a human would hit it too).
            vpx, vpy, verdict = await _verified_center(platform, elements, idx)
            through = may_click_through(verdict)
            if verdict != "hit" and not through:
                msg = (f"error: element #{idx} target {verdict} — "
                       f"skipping click (no dispatch)")
                log(msg)
                return msg
            if through:
                log(f"click-through {verdict} for element #{idx}")
            # The click's own humanized move is the settle onto the center.
            before_ids = platform.tab_ids()
            await asyncio.wait_for(page.mouse.click(vpx, vpy), timeout=10.0)
            msg = (
                f"element #{idx} scroll+circle+click at "
                f"({vpx / vp['width']:.3f},{vpy / vp['height']:.3f}) humanize=true"
            )
            if through:
                msg += f" through {_covering_tag(verdict)}"
            # Clicks often land in a fresh tab (target=_blank, popup): give
            # it a beat, then adopt it so the next perception reads the NEW
            # content instead of re-clicking this element forever.
            await asyncio.sleep(0.7)
            adopted = await platform.adopt_new_tab(before_ids)
            if adopted:
                msg += f" + newtab {adopted}"
            # Rebuild context immediately: clicks often expand content in
            # place instead of navigating, so re-snapshot the DOM now and
            # carry what changed (reads the adopted tab when there is one).
            snap = await _refresh_snapshot(platform)
            if snap:
                msg += f" + {snap}"
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
            vpx, vpy, verdict = await _verified_center(platform, elements, idx)
            if verdict != "hit":
                msg = (f"error: element #{idx} target {verdict} — "
                       f"skipping focus click (no dispatch, no typing)")
                log(msg)
                return msg
            await asyncio.wait_for(page.mouse.click(vpx, vpy), timeout=10.0)
            secret = _resolve_placeholders(text)
            await platform._harvest_and_accumulate(quiet=True)
            await _clear_field(platform)
            await page.keyboard.type(secret, delay=15)
            msg = (
                f"element #{idx} scroll+circle+click at "
                f"({vpx / vp['width']:.3f},{vpy / vp['height']:.3f}) humanize=true"
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
        msg = f"error: nav failed: {exc}"
        log(msg)
        return msg


def done_note_valid(note: str) -> bool:
    """done is only accepted with a non-empty outcome note."""
    return bool((note or "").strip())


__all__ = ["action_failed", "click_item", "type_at", "press_key", "goto_url", "done_note_valid", "may_click_through"]
