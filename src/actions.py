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
  if (!target) return {v: 'gone', k: ''};
  const tag = (target.tagName || '').toLowerCase();
  const kind = tag === 'a' ? 'link' : tag;
  const el = document.elementFromPoint(x, y);
  if (!el) return {v: 'void', k: kind};
  if (target === el || target.contains(el)) return {v: 'hit', k: kind};
  const cls = (el.getAttribute && el.getAttribute('class')) || '';
  return {v: 'covered:' + (el.tagName || '?').toLowerCase()
    + (cls ? '.' + String(cls).replace(/\\s+/g, ' ').slice(0, 40) : ''), k: kind};
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
    """Ask the DOM what is actually under the click point (verdict only)."""
    verdict, _kind = await _inspect_point(page, sel, px, py)
    return verdict


async def _inspect_point(page, sel: str, px: int, py: int) -> tuple[str, str]:
    """Ask the DOM what is under a point: (verdict, live element kind)."""
    try:
        res = await asyncio.wait_for(
            page.evaluate(POINT_CHECK_JS, {"sel": sel, "x": px, "y": py}),
            timeout=10.0,
        )
        if isinstance(res, dict):
            return str(res.get("v") or "void"), str(res.get("k") or "")
        return str(res or "void"), ""
    except Exception as exc:  # noqa: BLE001
        return f"error: point check failed: {exc}", ""


def stale_mismatch(expected: str | None, live: str) -> bool:
    """True when the live element is no longer what was decided on.

    data-jev idx tags are positional and re-assigned every probe; when the
    page re-renders between decide and act, idx N can point at a different
    element (seen live: result link became the search box). Refuse to act
    on a stale reference.
    """
    return bool(expected) and bool(live) and expected != live


def _sample_points(box: dict, vp: dict, n: int = 5) -> list[tuple[int, int]]:
    """Candidate click points across the box: center, then quadrants.

    A partial overlay (one span over the center) stops being a permanent
    block when corners still hit the target or an anchor.
    """
    w = max(1, int(vp.get("width", 1280)))
    h = max(1, int(vp.get("height", 800)))
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    dx = box["width"] * 0.3
    dy = box["height"] * 0.3
    pts: list[tuple[int, int]] = []
    for x, y in ((cx, cy), (cx - dx, cy - dy), (cx + dx, cy - dy),
                 (cx - dx, cy + dy), (cx + dx, cy + dy)):
        px = min(max(int(x), 0), w - 1)
        py = min(max(int(y), 0), h - 1)
        if (px, py) not in pts:
            pts.append((px, py))
    return pts[: max(1, n)]


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


async def _verified_center(platform, elements: list[ElementRef], idx: int,
                           samples: int = 5):
    """Fresh box + sampled point checks with one re-scroll retry.

    Returns (px, py, verdict, live_kind). Prefers a direct hit, then a
    clickable cover (anchor/button), else the last verdict seen.
    """
    page = platform.page
    sel = f'[data-jev="{idx}"]'
    vp = page.viewport_size or {"width": 1280, "height": 800}
    last: tuple = (None, None, "gone", "")
    for _ in (1, 2):
        found = await _element_center(platform, elements, idx)
        if not found:
            last = (None, None, "gone", "")
            continue
        _box = found[2]
        fallback = None
        for px, py in _sample_points(_box, vp, samples):
            verdict, live_kind = await _inspect_point(page, sel, px, py)
            if verdict == "hit":
                return px, py, verdict, live_kind
            if verdict.startswith("error:"):
                return px, py, verdict, live_kind
            if fallback is None and may_click_through(verdict):
                fallback = (px, py, verdict, live_kind)
            last = (px, py, verdict, live_kind)
        if fallback is not None:
            return fallback
    return last


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


async def click_item(platform, elements: list[ElementRef], idx: int,
                 expected_kind: str | None = None) -> str:
    """Scroll + human-loop highlight + verified click on element idx.

    expected_kind is the kind decided on; when the live element no longer
    matches (map re-rendered between decide and act), the reference is stale
    and no click is dispatched.
    """
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
            vpx, vpy, verdict, live_kind = await _verified_center(platform, elements, idx)
            through = may_click_through(verdict)
            if stale_mismatch(expected_kind, live_kind):
                msg = (f"error: element #{idx} stale map (decided {expected_kind}, "
                       f"live {live_kind or 'gone'}) — skipping click (no dispatch)")
                log(msg)
                return msg
            if verdict != "hit" and not through:
                msg = (f"error: element #{idx} target {verdict} — "
                       f"skipping click (no dispatch)")
                log(msg)
                return msg
            if through:
                log(f"click-through {verdict} for element #{idx}")
            # The click's own humanized move is the settle onto the center.
            before_ids = platform.tab_ids()
            try:
                prev_url = page.url
            except Exception:
                prev_url = ""
            await asyncio.wait_for(page.mouse.click(vpx, vpy), timeout=10.0)
            msg = (
                f"element #{idx} scroll+circle+click at "
                f"({vpx / vp['width']:.3f},{vpy / vp['height']:.3f}) humanize=true"
            )
            if through:
                msg += f" through {_covering_tag(verdict)}"
            # Wait for the click's own effect (new tab, same-tab nav, or
            # nothing) instead of a blind sleep, then rebuild context.
            outcome, info = await platform.settle_after_action(prev_url, before_ids)
            if outcome == "newtab" and info:
                msg += f" + newtab {info}"
            elif outcome == "navigated" and info:
                msg += f" + navigated {info}"
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


async def type_at(platform, elements: list[ElementRef], idx: int, text: str,
              expected_kind: str | None = None) -> str:
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
            vpx, vpy, verdict, live_kind = await _verified_center(platform, elements, idx)
            if stale_mismatch(expected_kind, live_kind):
                msg = (f"error: element #{idx} stale map (decided {expected_kind}, "
                       f"live {live_kind or 'gone'}) — skipping focus click (no dispatch, no typing)")
                log(msg)
                return msg
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
    """Press Enter/Tab/Escape after typing (e.g. Enter to submit).

    Enter submits forms and usually navigates, so it settles like a click;
    Tab/Escape just dispatch.
    """
    async with platform._lock:
        paused = await platform.quiesce_idle_motion()
        try:
            await platform._harvest_and_accumulate(quiet=True)
            before_ids = platform.tab_ids()
            try:
                prev_url = platform.page.url
            except Exception:
                prev_url = ""
            await platform.page.keyboard.press(key)
            msg = f"key={key}"
            if key == "Enter":
                outcome, info = await platform.settle_after_action(prev_url, before_ids)
                if outcome == "newtab" and info:
                    msg += f" + newtab {info}"
                elif outcome == "navigated" and info:
                    msg += f" + navigated {info}"
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
