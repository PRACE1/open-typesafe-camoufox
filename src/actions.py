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
from .capability.resolve import (
    TEXT_ENTRY_KINDS,
    _confirm_aria_ref,
    _kinds_compatible,
    _labels_compatible,
    _remap_idx,
    _resolve_by_aria_ref,
    _resolve_by_selector,
    _resolve_target,
    _slot_changed,
    heal_target,
    read_input_value,
)
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


POINT_CHECK_JS = """({x, y, expRole, expName}) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return {v: 'void', k: '', n: ''};
  const tag = (el.tagName || '').toLowerCase();
  const kind = tag === 'a' ? 'link' : tag;
  const nm = ((el.getAttribute && (el.getAttribute('aria-label') || '')) || el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
  const exp = String(expName || '').trim().toLowerCase();
  const low = nm.toLowerCase();
  const nameOk = !exp || low === exp || (exp && low.includes(exp)) || (exp && exp.includes(low));
  if (kind === String(expRole || '') && nameOk) return {v: 'hit', k: kind, n: nm};
  const cls = (el.getAttribute && el.getAttribute('class')) || '';
  return {v: 'covered:' + tag
    + (cls ? '.' + String(cls).replace(/\\s+/g, ' ').slice(0, 40) : ''), k: kind, n: nm};
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


async def _point_status(page, px: int, py: int,
                      exp_role: str = "", exp_name: str = "") -> str:
    """Ask the DOM what is actually under the click point (verdict only)."""
    verdict, _kind = await _inspect_point(page, px, py, exp_role, exp_name)
    return verdict


async def _inspect_point(page, px: int, py: int,
                         exp_role: str = "", exp_name: str = "") -> tuple[str, str]:
    """Ask the DOM what is under a point: (verdict, live element kind).

    Selector-free: elementFromPoint plus a role/name cross-check against the
    expected target. No DOM markers are ever queried (stealth).
    """
    try:
        res = await asyncio.wait_for(
            page.evaluate(POINT_CHECK_JS, {
                "x": px, "y": py, "expRole": exp_role, "expName": exp_name}),
            timeout=10.0,
        )
        if isinstance(res, dict):
            return str(res.get("v") or "void"), str(res.get("k") or "")
        return str(res or "void"), ""
    except Exception as exc:  # noqa: BLE001
        return f"error: point check failed: {exc}", ""


def stale_mismatch(expected: str | None, live: str) -> bool:
    """True when the live element is incompatibly different from decided.

    Delegates to _kinds_compatible: text-entry granularity drift between
    the aria and DOM vocabularies (input vs textarea for one widget) is
    not staleness. Empty sides carry no signal.
    """
    return bool(expected) and bool(live) and not _kinds_compatible(expected, live)


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


async def _scroll_box_into_view(platform, box: dict) -> None:
    """Coordinate scroll (no selectors): bring an off-screen box on screen.

    Fail-soft: any failure leaves the viewport alone and the caller treats
    the target as not actionable this step.
    """
    try:
        page = platform.page
        vp = page.viewport_size or {"width": 1280, "height": 800}
        vh = int(vp.get("height", 800))
        if 0 <= box["y"] <= vh:
            return
        await page.evaluate("(dy) => window.scrollBy(0, dy)",
                            box["y"] - vh / 3)
        await asyncio.sleep(0.3)
    except Exception:  # noqa: BLE001
        pass


async def _verified_center(platform, elements: list[ElementRef], idx: int,
                           samples: int = 5):
    """Fresh box + sampled point checks with scroll + one re-resolve retry.

    Returns (px, py, verdict, live_kind). Prefers a direct hit, then a
    clickable cover (anchor/button), else the last verdict seen. A shifted
    map reports stale (no dispatch); a missing slot reports gone.
    Compatible granularity drift (input vs textarea for one widget) is
    verified against the LIVE identity instead of refused. An ok resolver
    self-hit (the node itself under its center) returns hit immediately —
    vocabulary drift in sampled point checks cannot false-cover it.
    (Self-hit never overrides a stale verdict.)
    """
    page = platform.page
    vp = page.viewport_size or {"width": 1280, "height": 800}
    last: tuple = (None, None, "gone", "")
    for _ in (1, 2):
        res = await _resolve_target(platform, elements, idx)
        if res["status"] == "gone" or res["box"] is None:
            last = (None, None, "gone", "")
            continue
        if res["status"] == "ok" and res.get("self_hit"):
            box = res["box"]
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            vw = max(1, int(vp.get("width", 1280)))
            vh = max(1, int(vp.get("height", 800)))
            return (min(max(int(cx), 0), vw - 1),
                    min(max(int(cy), 0), vh - 1),
                    "hit", res["live_kind"])
        if res["status"] == "stale":
            old = next((e for e in elements if e.idx == idx), None)
            drift = (
                old is not None
                and _kinds_compatible(old.kind, res["live_kind"])
                and _labels_compatible(
                    old.label or old.placeholder or old.text or old.id or "",
                    res["live_label"]))
            if not drift:
                return None, None, f"stale:{res['live_kind']}", res["live_kind"]
            # Same widget, coarser label: verify points, don't refuse.
            el_kind, exp_name = res["live_kind"], res["live_label"]
        else:
            el = res["element"]
            el_kind = el.kind
            exp_name = (el.label or el.placeholder or el.text or el.id or "")
        box = res["box"]
        vh = int(vp.get("height", 800))
        if not (0 <= box["y"] <= vh):
            await _scroll_box_into_view(platform, box)
            continue
        fallback = None
        for px, py in _sample_points(box, vp, samples):
            verdict, live_kind = await _inspect_point(
                page, px, py, el_kind, exp_name)
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


async def probe_target(platform, elements: list[ElementRef], idx: int,
                     expected_kind: str | None = None) -> dict:
    """Pre-flight probe of element idx: resolve, verify, classify — no dispatch.

    Returns a dict with status (ok/through/covered/stale/gone/error),
    verdict, px/py, box (px dict, for annotation banking), live_kind, and
    label. The runner calls this on its heal pass; click_item uses it
    before every dispatch.
    """
    el = next((e for e in elements if e.idx == idx), None)
    blank = {"idx": idx, "status": "gone", "verdict": "gone",
             "px": None, "py": None, "box": None, "live_kind": "", "label": ""}
    if el is None:
        return blank
    label = el.label or el.placeholder or el.text or el.id or f"element #{idx}"
    res = await _resolve_target(platform, elements, idx)
    if res["status"] == "gone":
        return {**blank, "label": label}
    if res["status"] == "stale":
        return {"idx": idx, "status": "stale",
                "verdict": f"stale:{res['live_kind']}", "px": None, "py": None,
                "box": None, "live_kind": res["live_kind"], "label": label}
    px, py, verdict, live_kind = await _verified_center(platform, elements, idx)
    if verdict.startswith("stale:"):
        return {"idx": idx, "status": "stale", "verdict": verdict,
                "px": px, "py": py, "box": res["box"],
                "live_kind": live_kind, "label": label}
    if px is None or verdict == "gone":
        return {**blank, "label": label}
    if verdict.startswith("error:"):
        return {"idx": idx, "status": "error", "verdict": verdict,
                "px": px, "py": py, "box": res["box"],
                "live_kind": live_kind, "label": label}
    if stale_mismatch(expected_kind, live_kind):
        return {"idx": idx, "status": "stale", "verdict": verdict,
                "px": px, "py": py, "box": res["box"],
                "live_kind": live_kind, "label": label}
    if verdict == "hit":
        status = "ok"
    elif may_click_through(verdict):
        status = "through"
    else:
        status = "covered"
    return {"idx": idx, "status": status, "verdict": verdict,
            "px": px, "py": py, "box": res["box"],
            "live_kind": live_kind, "label": label}


def _bank_box(elements: list[ElementRef], idx: int,
              box_px: dict | None, vp: dict) -> None:
    """Bank the resolved px box (normalized) onto the step's element.

    Lets report.py draw the acted element's true rect; elements never
    resolved keep box=None and are skipped in annotation.
    """
    if not box_px:
        return
    el = next((e for e in elements if e.idx == idx), None)
    if el is None:
        return
    try:
        vw = max(1, int(vp.get("width", 1280)))
        vh = max(1, int(vp.get("height", 800)))
        el.box = (box_px["x"] / vw, box_px["y"] / vh,
                  box_px["width"] / vw, box_px["height"] / vh)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        pass


async def dispatch_verified_click(platform, px: int, py: int,
                                  timeout: float = 10.0,
                                  harvest: bool = True) -> dict:
    """Click an already-verified point, settle, and rebuild context.

    Lock-free: the caller must hold platform._lock and have harvested the
    buffer first (or pass harvest=True). Returns outcome/info/snapshot.
    """
    page = platform.page
    if harvest:
        await platform._harvest_and_accumulate(quiet=True)
    before_ids = platform.tab_ids()
    try:
        prev_url = page.url
    except Exception:  # noqa: BLE001
        prev_url = ""
    try:
        await asyncio.wait_for(page.mouse.click(px, py), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"outcome": "error", "info": f"dispatch failed: {exc}",
                "snapshot": ""}
    outcome, info = await platform.settle_after_action(prev_url, before_ids)
    snap = await _refresh_snapshot(platform)
    return {"outcome": outcome, "info": info or "", "snapshot": snap}


async def _dismiss_cover(platform) -> bool:
    """Dismiss a covering overlay once: Escape, then let animations settle.

    Radix-style menus/dialogs (DismissableLayer) close on Escape, as do
    most dropdowns and toasts. Fail-soft: False when the keypress itself
    fails. The caller re-verifies afterwards — never assume it worked.
    """
    try:
        await platform.page.keyboard.press("Escape")
        await asyncio.sleep(0.6)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"dismiss cover: {exc}")
        return False


async def verify_for_dispatch(platform, elements: list[ElementRef], idx: int,
                              expected_kind: str | None = None) -> tuple[dict, bool]:
    """Probe a target, recovering from a covering overlay once.

    Returns (probe, dismissed). When the first probe reports covered, the
    overlay is dismissed (Escape) and the point re-verified a single time —
    the standard playbook for intercepted clicks: dismiss explicitly,
    never force-click through. One recovery only; a still-covered point
    stays a refusal (the runner's heal layer owns the retry).
    """
    probe = await probe_target(platform, elements, idx, expected_kind)
    if probe["status"] != "covered":
        return probe, False
    log(f"cover on element #{idx} ({probe['verdict']}) — dismissing once (Escape)")
    if not await _dismiss_cover(platform):
        return probe, False
    fresh = await probe_target(platform, elements, idx, expected_kind)
    log(f"cover dismissed, re-verified element #{idx}: {fresh['verdict']}")
    return fresh, True


async def _element_center(platform, elements: list[ElementRef], idx: int):
    """Resolve ref idx to a visible-box center; return (px, py, box) or None.

    Selector-free: resolution runs through _resolve_target (fresh probe +
    cross-check), scrolling by coordinates when the box is off-screen.
    None covers gone, stale, and still-off-screen — point verification
    downstream distinguishes the diagnosis.
    """
    page = platform.page
    vp = page.viewport_size or {"width": 1280, "height": 800}
    for _ in (1, 2):
        res = await _resolve_target(platform, elements, idx)
        if res["status"] != "ok" or res["box"] is None:
            return None
        box = res["box"]
        vh = int(vp.get("height", 800))
        if not (0 <= box["y"] <= vh):
            await _scroll_box_into_view(platform, box)
            continue
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        px = min(max(int(cx), 0), int(vp["width"]) - 1)
        py = min(max(int(cy), 0), int(vp["height"]) - 1)
        return px, py, box
    return None


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
            # Prove the point hits the target (layout shifts and overlays in
            # between mean a blind click dispatches yet activates nothing),
            # then dispatch through the verified-click atom. A covering
            # overlay is dismissed once (Escape) and re-verified — our own
            # hover can open hover-triggered menus onto the click point.
            probe, dismissed = await verify_for_dispatch(platform, elements, idx, expected_kind)
            verdict, live_kind = probe["verdict"], probe["live_kind"]
            status = probe["status"]
            through = status == "through"
            if status == "stale":
                msg = (f"error: element #{idx} stale map (decided {expected_kind}, "
                       f"live {live_kind or 'gone'}) — skipping click (no dispatch)")
                log(msg)
                return msg
            if status in ("gone", "covered", "error"):
                msg = (f"error: element #{idx} target {verdict} — "
                       f"skipping click (no dispatch)")
                log(msg)
                return msg
            if through:
                log(f"click-through {verdict} for element #{idx}")
            # The click's own humanized move is the settle onto the center.
            vpx, vpy = probe["px"], probe["py"]
            _bank_box(elements, idx, probe.get("box"), vp)
            res = await dispatch_verified_click(platform, vpx, vpy, harvest=False)
            if res["outcome"] == "error":
                msg = f"error clicking element #{idx}: {res['info']}"
                log(msg)
                return msg
            msg = (
                f"element #{idx} scroll+circle+click at "
                f"({vpx / vp['width']:.3f},{vpy / vp['height']:.3f}) humanize=true"
            )
            if through:
                msg += f" through {_covering_tag(verdict)}"
            if dismissed:
                msg += " + cover dismissed via Escape"
            # Wait for the click's own effect (new tab, same-tab nav, or
            # nothing) instead of a blind sleep, then rebuild context.
            outcome, info, snap = res["outcome"], res["info"], res["snapshot"]
            if outcome == "newtab" and info:
                msg += f" + newtab {info}"
            elif outcome == "navigated" and info:
                msg += f" + navigated {info}"
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


def _challenge_kind(el: ElementRef) -> str:
    """Classify a challenge control: slider / checkbox / captcha / none."""
    blob = f"{el.kind} {el.type} {el.label} {el.placeholder} {el.text} {el.id}".lower()
    if any(k in blob for k in ("captcha", "recaptcha", "puzzle", "verify you are human",
                               "i'm not a robot", "not a robot")):
        return "captcha"
    if "slider" in blob or "slide to" in blob or "drag" in blob:
        return "slider"
    if "checkbox" in blob or el.kind == "checkbox":
        return "checkbox"
    return "none"


async def challenge_control(platform, elements: list[ElementRef], idx: int,
                            expected_kind: str | None = None) -> str:
    """Work a checkbox/slider challenge; refuse image CAPTCHAs out loud.

    Checkbox: verified click. Slider: humanized drag across the track, then
    settle like a click (they usually submit). Image/puzzle CAPTCHA: no solve
    attempted — returns an escalating error so the runner stops with a note
    instead of burning no-ops.
    """
    async with platform._lock:
        paused = await platform.quiesce_idle_motion()
        try:
            page = platform.page
            el = next((e for e in elements if e.idx == idx), None)
            if el is None:
                msg = f"error: challenge #{idx} gone — skipping (no dispatch)"
                log(msg)
                return msg
            kind = _challenge_kind(el)
            if kind == "captcha":
                msg = (f"error: element #{idx} captcha/image challenge — "
                       f"escalating to done (no solve attempted)")
                log(msg)
                return msg
            if kind == "none":
                msg = (f"error: element #{idx} is not a checkbox/slider "
                       f"(live kind {el.kind}) — skipping challenge (no dispatch)")
                log(msg)
                return msg
            probe, dismissed = await verify_for_dispatch(platform, elements, idx, expected_kind)
            if probe["status"] not in ("ok", "through"):
                msg = (f"error: element #{idx} target {probe['verdict']} — "
                       f"skipping challenge (no dispatch)")
                log(msg)
                return msg
            if kind == "checkbox":
                res = await dispatch_verified_click(
                    platform, probe["px"], probe["py"])
                if res["outcome"] == "error":
                    msg = f"error toggling challenge checkbox #{idx}: {res['info']}"
                    log(msg)
                    return msg
                _bank_box(elements, idx, probe.get("box"),
                          page.viewport_size or {"width": 1280, "height": 800})
                msg = f"element #{idx} challenge checkbox toggled humanize=true"
                if dismissed:
                    msg += " + cover dismissed via Escape"
                if res["outcome"] in ("newtab", "navigated") and res["info"]:
                    msg += f" + {res['outcome']} {res['info']}"
                if res["snapshot"]:
                    msg += f" + {res['snapshot']}"
                log(msg)
                return msg
            # Slider: drag the handle across the track in small human steps.
            found = await _element_center(platform, elements, idx)
            if not found:
                msg = f"error: element #{idx} has no bounding box (hidden?)"
                log(msg)
                return msg
            _px, _py, box = found
            x0 = box["x"] + box["width"] * 0.15
            x1 = box["x"] + box["width"] * 0.9
            cy = box["y"] + box["height"] / 2
            await platform._harvest_and_accumulate(quiet=True)
            before_ids = platform.tab_ids()
            try:
                prev_url = page.url
            except Exception:  # noqa: BLE001
                prev_url = ""
            mouse = page.mouse
            await mouse.move(x0, cy)
            await mouse.down()
            try:
                for i in range(1, 9):
                    x = x0 + (x1 - x0) * i / 8
                    await mouse.move(x, cy)
                    await asyncio.sleep(0.05)
            finally:
                await mouse.up()
            outcome, info = await platform.settle_after_action(prev_url, before_ids)
            snap = await _refresh_snapshot(platform)
            _bank_box(elements, idx, box,
                      page.viewport_size or {"width": 1280, "height": 800})
            msg = f"element #{idx} slider drag 8 steps humanize=true"
            if outcome in ("newtab", "navigated") and info:
                msg += f" + {outcome} {info}"
            if snap:
                msg += f" + {snap}"
            log(msg)
            return msg
        except Exception as exc:  # noqa: BLE001
            msg = f"error working challenge #{idx}: {exc}"
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
            _bank_box(elements, idx, box, vp)
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


__all__ = ["action_failed", "click_item", "type_at", "press_key", "goto_url", "done_note_valid", "may_click_through",
           "probe_target", "dispatch_verified_click", "verify_for_dispatch", "heal_target", "challenge_control"]
