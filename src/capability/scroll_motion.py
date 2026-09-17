"""
Async scroll execution against a real Playwright page or nested scroll
container. Builds on the pure trajectories from scroll_math.py and drives
them via page.mouse.wheel() (page-level) or direct scrollTop assignment
(nested containers).

Never uses scroll_into_view_if_needed as the main movement — it jumps
instantly and ruins the recording; it's only used as a final sub-pixel
correction elsewhere.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

from .logging_utils import log, log_verbose
from .scroll_math import ScrollStep, human_scroll_trajectory

async def resolve_scroll_owner(
    page: Any,
    locator: Any,
) -> tuple[bool, dict[str, Any]]:
    """Determine whether scrolling the target requires page or container logic.

    Walks up the DOM from the target element and returns the nearest scrollable
    ancestor (or None for page-level). Returns ``(is_window, geometry)``.

    For page-level scrolls (is_window=True), geometry is ``{isWindow: true}`` —
    callers must read window.scrollY / innerHeight / scrollHeight themselves.

    For nested containers (is_window=False), geometry has keys:
      - isWindow: false
      - scrollTop: current scroll position
      - clientHeight: visible height
      - scrollHeight: total scrollable height
      - hoverX / hoverY: centre of the container's bounding box
      - ownerSelector: a CSS path to the scroll container (nth-child chain),
        so callers can build a fresh locator for it — passing the target's
        locator as a second arg to locator.evaluate() serializes it, it does
        NOT give the JS callback a live DOM reference to the container.
    """
    result = await locator.evaluate("""
        (el) => {
            let cur = el;
            let depth = 0;
            while (cur && cur !== document.documentElement) {
                const s = getComputedStyle(cur);
                if ((s.overflowY === 'auto' || s.overflowY === 'scroll' || s.overflowY === 'hidden')
                    && cur.scrollHeight > cur.clientHeight) {
                    const r = cur.getBoundingClientRect();
                    // Build a CSS selector chain back to this node so the
                    // caller can re-locate it. nth-child is stable for
                    // same-session navigation; we can't return a live
                    // ElementHandle across an evaluate boundary reliably.
                    let path = [];
                    let node = cur;
                    while (node && node !== document.documentElement) {
                        let part = node.tagName.toLowerCase();
                        if (node.id) {
                            part = '#' + node.id;
                            path.unshift(part);
                            break;
                        }
                        const parent = node.parentElement;
                        if (parent) {
                            let idx = 1;
                            let sibling = parent.firstElementChild;
                            while (sibling && sibling !== node) {
                                idx++;
                                sibling = sibling.nextElementSibling;
                            }
                            part += ':nth-child(' + idx + ')';
                        }
                        path.unshift(part);
                        node = parent;
                    }
                    return {
                        isWindow: false,
                        scrollTop: cur.scrollTop,
                        clientHeight: cur.clientHeight,
                        scrollHeight: cur.scrollHeight,
                        hoverX: r.left + r.width / 2,
                        hoverY: r.top + r.height / 2,
                        ownerSelector: path.join(' > '),
                    };
                }
                cur = cur.parentElement;
                depth++;
            }
            return { isWindow: true };
        }
    """)
    return result["isWindow"], result


async def walk_segments_page(
    page: Any,
    steps: list[ScrollStep],
    hover_x: float | None = None,
    hover_y: float | None = None,
) -> None:
    """Execute planned segments on the page via page.mouse.wheel()."""
    try:
        vp = page.viewport_size or {"width": 1280, "height": 800}
        mx = hover_x if hover_x is not None else vp["width"] * 0.5 + random.uniform(-60, 60)
        my = hover_y if hover_y is not None else vp["height"] * 0.45 + random.uniform(-50, 50)
        await asyncio.wait_for(page.mouse.move(mx, my), timeout=5.0)
    except (Exception, asyncio.TimeoutError) as exc:
        log_verbose(f"scroll: could not pre-position mouse: {exc}")

    for i, step in enumerate(steps, 1):
        try:
            await page.mouse.wheel(0, step.delta_y)
        except Exception as exc:
            log_verbose(f"scroll step {i}: wheel event failed ({type(exc).__name__}), breaking loop")
            break
        await asyncio.sleep(step.delay_ms / 1000)
        if i <= 3 or i % 10 == 0 or i == len(steps):
            log_verbose(f"scroll step {i}/{len(steps)}: delta={step.delta_y:.1f}, "
                         f"delay={step.delay_ms}ms")


async def walk_segments_container(
    owner_locator: Any,
    steps: list[ScrollStep],
    hover_x: float | None = None,
    hover_y: float | None = None,
) -> None:
    """Execute planned segments on a nested container via direct scrollTop.

    ``owner_locator`` is a locator for the scroll container (not the target
    element). The caller builds it from the ownerSelector returned by
    resolve_scroll_owner — passing the *target* locator here would scroll
    the target, not the container, because ``el`` in the evaluate callback
    would be the target element, not the scrollable ancestor.
    """
    page = owner_locator.page
    # Park cursor over container so the visible cursor reads as "on the wheel".
    if hover_x is not None and hover_y is not None:
        try:
            await asyncio.wait_for(page.mouse.move(hover_x, hover_y), timeout=5.0)
        except (Exception, asyncio.TimeoutError):
            pass

    for i, step in enumerate(steps, 1):
        try:
            await owner_locator.evaluate(
                """(el, d) => { el.scrollTop += d; }""",
                step.delta_y,
            )
        except Exception as exc:
            log_verbose(f"scroll step {i}: scrollTop assignment failed ({type(exc).__name__}), breaking loop")
            break
        await asyncio.sleep(step.delay_ms / 1000)
        if i <= 3 or i % 10 == 0 or i == len(steps):
            log_verbose(f"scroll step {i}/{len(steps)}: delta={step.delta_y:.1f}, "
                         f"delay={step.delay_ms}ms")


async def human_scroll_by(
    page: Any,
    distance: float,
    *,
    seed: int | None = None,
) -> None:
    """Execute a human-like scroll trajectory via wheel deltas.

    Moves the mouse into the viewport first (so wheel events register), then
    emits each ScrollStep as a page.mouse.wheel() call with the step's delay.
    """
    steps = human_scroll_trajectory(distance, seed=seed)
    if not steps:
        return

    log_verbose(f"scroll trajectory: distance={distance:.0f}px, steps={len(steps)}, "
         f"duration~={sum(s.delay_ms for s in steps)}ms")

    await walk_segments_page(page, steps)


async def scroll_to_ref(
    page: Any,
    selector: str,
    *,
    seed: int | None = None,
) -> dict[str, Any]:
    """Scroll a selector into view using a trajectory, then verify.

    Never uses scroll_into_view_if_needed as the main movement (it jumps
    instantly and ruins the recording). Instead: resolve the scroll owner
    (page vs nested container), generate a bell-curve trajectory, execute
    wheel deltas or direct scrollTop assignment, and verify visibility.

    Returns a small result dict for the agent and for logging.
    """
    locator = page.locator(selector).first
    await locator.wait_for(state="attached", timeout=3000)

    is_window, geom = await resolve_scroll_owner(page, locator)

    # Read the current scroll position and max scroll in the owner's coordinate
    # system. resolve_scroll_owner returns {isWindow: true} (no further keys)
    # for page-level scrolls and {isWindow:false, scrollTop, clientHeight,
    # scrollHeight, hoverX, hoverY, ownerSelector} for nested containers.
    if is_window:
        before = await page.evaluate("window.scrollY")
        max_scroll = await page.evaluate(
            "Math.max(0, document.documentElement.scrollHeight - window.innerHeight)"
        )
    else:
        before = geom["scrollTop"]
        max_scroll = max(0, geom["scrollHeight"] - geom["clientHeight"])

    # Calculate target position in the same coordinate system as ``before``.
    # For page scrolls: absolute Y minus 28% viewport offset (matches old behaviour).
    # For container scrolls: element offset inside container, centred.
    if is_window:
        target_y = await locator.evaluate("""
            el => {
                const rect = el.getBoundingClientRect();
                const absoluteY = rect.top + window.scrollY;
                const preferredOffset = window.innerHeight * 0.28;
                return Math.max(0, absoluteY - preferredOffset);
            }
        """)
    else:
        # Build a locator for the scroll container from ownerSelector so we
        # can evaluate on it. Passing the *target* locator as a second arg
        # to locator.evaluate() serializes it — the JS callback gets a plain
        # object, not a live DOM reference to the container.
        owner_sel = geom.get("ownerSelector", "")
        owner_loc = page.locator(owner_sel).first if owner_sel else None
        if owner_loc is None:
            log(f"scroll_to_ref: no ownerSelector for nested container, "
                f"falling back to page scroll")
            is_window = True
            before = await page.evaluate("window.scrollY")
            max_scroll = await page.evaluate(
                "Math.max(0, document.documentElement.scrollHeight - window.innerHeight)"
            )
            target_y = await locator.evaluate("""
                el => {
                    const rect = el.getBoundingClientRect();
                    const absoluteY = rect.top + window.scrollY;
                    const preferredOffset = window.innerHeight * 0.28;
                    return Math.max(0, absoluteY - preferredOffset);
                }
            """)
        else:
            target_y = await owner_loc.evaluate("""
                (container) => {
                    const el = arguments[1];
                    const cRect = container.getBoundingClientRect();
                    const eRect = el.getBoundingClientRect();
                    const relStart = eRect.top - cRect.top;
                    const absStart = relStart + container.scrollTop;
                    const preferredOffset = container.clientHeight * 0.28;
                    return Math.max(0, absStart - preferredOffset);
                }
            """, await locator.element_handle())

    # Clamp target to the document's maximum scroll position so an element
    # near the bottom doesn't request a distance the browser can't consume.
    target_y = min(target_y, max_scroll)
    distance = target_y - before
    log_verbose(f"scroll_to_ref: selector={selector}, owner={'page' if is_window else 'container'}, "
         f"before={before:.0f}, target={target_y:.0f}, distance={distance:.0f}px")

    steps = human_scroll_trajectory(distance, seed=seed)
    if not steps:
        log("scroll_to_ref: zero distance, nothing to scroll")
        after = before
        visible = True
    elif is_window:
        await walk_segments_page(page, steps)
        after = await page.evaluate("window.scrollY")
    else:
        hover_x = geom.get("hoverX")
        hover_y = geom.get("hoverY")
        # Use the owner locator (scroll container), not the target locator.
        # walk_segments_container calls owner.evaluate("(el, d) => { el.scrollTop += d }")
        # which scrolls the container; passing the target locator would try
        # to set scrollTop on the target element (which isn't scrollable).
        await walk_segments_container(owner_loc, steps, hover_x, hover_y)
        after = await owner_loc.evaluate("el => el.scrollTop")

    # Final visibility check: is the element actually in the viewport?
    # For container scrolls, check against the container's bounds instead of
    # the window viewport. We use owner_loc.evaluate with the target's
    # ElementHandle as a second arg — evaluate serializes ElementHandle to
    # a live DOM element in the callback.
    if is_window:
        visible = await locator.evaluate("""
            el => {
                const r = el.getBoundingClientRect();
                return r.top >= 0 &&
                       r.bottom <= window.innerHeight &&
                       r.height > 0;
            }
        """)
    else:
        target_handle = await locator.element_handle()
        visible = await owner_loc.evaluate("""
            (container) => {
                const el = arguments[1];
                const r = el.getBoundingClientRect();
                const cR = container.getBoundingClientRect();
                return r.top >= cR.top &&
                       r.bottom <= cR.bottom &&
                       r.height > 0;
            }
        """, target_handle)

    log(f"scroll_to_ref: result ok={visible}, before={before:.0f}, after={after:.0f}, "
         f"distance={distance:.0f}")

    return {
        "ok": visible,
        "ref": selector,
        "scroll_y_before": before,
        "scroll_y_after": after,
        "distance": distance,
    }
