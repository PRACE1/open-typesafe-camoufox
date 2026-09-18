"""
actions.py — one deterministic handler per action kind (typesafe's actions.py).

Each handler drives the platform adapter and returns a history line.
Free text arrives already composed (writer.py) or as an {ENV} placeholder
resolved here at execution — values never enter prompts; logs carry
length-masked placeholders only.
"""

from __future__ import annotations

import asyncio

from .capability.aria_refs import resolve_ref
from .capability.logging_utils import log as _base_log


def log(msg: str, tag: str = "actions") -> None:
    """Hierarchical domain logger (default ``actions[:sub]``)."""
    _base_log(msg, tag=tag)
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
                "frame_embedded": _covering_tag(verdict) == "iframe",
                "live_kind": live_kind, "label": label}
    if verdict == "hit":
        status = "ok"
    elif may_click_through(verdict):
        status = "through"
    else:
        status = "covered"
    return {"idx": idx, "status": status, "verdict": verdict,
            "px": px, "py": py, "box": res["box"],
            "frame_embedded": _covering_tag(verdict) == "iframe",
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
                                  harvest: bool = True,
                                  native_aria: str | None = None,
                                  force: bool = False) -> dict:
    """Click an already-verified point, settle, and rebuild context.

    Lock-free: the caller must hold platform._lock and have harvested the
    buffer first (or pass harvest=True). Returns outcome/info/snapshot.

    native_aria switches the dispatch to a native locator click: framed
    targets (recaptcha checkboxes) live behind an iframe boundary that
    elementFromPoint cannot pierce, so coordinate clicks can never verify
    there — Playwright resolves the frame natively instead. Humanized
    motion is skipped for these; the verified identity + settle + snapshot
    rails still apply.

    force skips Playwright actionability (visibility/stability/enabled)
    waits on the native path — shield checkboxes animate continuously,
    so a stability wait can hang past the timeout without the click ever
    dispatching (seen live: empty dispatch failure on /sorry/). Only the
    challenge-checkbox path opts in; coordinate clicks keep full checks.
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
        if native_aria is not None:
            locator = page.locator(resolve_ref(native_aria, {native_aria}))
            await asyncio.wait_for(
                locator.click(timeout=int(timeout * 1000), force=force),
                timeout=timeout)
        else:
            await asyncio.wait_for(page.mouse.click(px, py), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        # Class name included: bare TimeoutError stringifies empty, which
        # used to produce undebuggable "dispatch failed: " lines.
        return {"outcome": "error",
                "info": f"dispatch failed: {exc.__class__.__name__}: {exc}",
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

    Returns (probe, dismissed). When the first probe reports covered —
    by status, or by verdict when the status is stale (kind drift over
    a covered point, e.g. checkbox-vs-div on a bot-check page) — the
    overlay is dismissed (Escape) and the point re-verified a single
    time: the standard playbook for intercepted clicks, dismiss
    explicitly, never force-click through. One recovery only; a
    still-covered point stays a refusal (the runner's heal layer owns
    the retry).
    """
    probe = await probe_target(platform, elements, idx, expected_kind)
    if probe["status"] != "covered" \
            and not str(probe.get("verdict") or "").startswith("covered:"):
        return probe, False
    log(f"target=#{idx} cover={probe['verdict']} action=escape-once",
        tag="actions:cover")
    if not await _dismiss_cover(platform):
        return probe, False
    fresh = await probe_target(platform, elements, idx, expected_kind)
    log(f"target=#{idx} reverdict={fresh['verdict']}",
        tag="actions:cover")
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
            # Framed targets (recaptcha checkboxes) sit behind an iframe
            # boundary elementFromPoint cannot pierce: dispatch natively via
            # the aria-ref locator instead of refusing. This covers both
            # covered AND stale verdicts — a decided checkbox probing as a
            # live iframe is the normal shape of an embedded widget, not a
            # re-rendered map (seen live: every click refused forever).
            tgt = next((e for e in elements if e.idx == idx), None)
            native = (tgt.aria if tgt is not None
                      and status in ("covered", "stale")
                      and probe.get("frame_embedded") and tgt.aria else None)
            if native:
                # An open image-grid follow-up must survive: clicking the
                # anchor now would close its popup, so refuse loudly and
                # let heal route to challenge_control's OCR capture.
                from .capability.shield_solve import detect_image_followup
                try:
                    _page_text = await get_page_text(platform, limit=800)
                except Exception:  # noqa: BLE001
                    _page_text = ""
                _open, _via = await detect_image_followup(
                    platform, _page_text)
                if _open:
                    msg = (f"error: element #{idx} image follow-up open "
                           f"({_via}) — anchor click would close it, no "
                           f"dispatch (route via challenge)")
                    log(f"target=#{idx} state=open via={_via} "
                        f"decision=refuse-anchor-click",
                        tag="actions:click")
                    return msg
                log(f"target=#{idx} locator=native ref={native}",
                    tag="actions:click")
            if status == "stale" and native is None:
                msg = (f"error: element #{idx} stale map (decided {expected_kind}, "
                       f"live {live_kind or 'gone'}) — skipping click (no dispatch)")
                log(msg)
                return msg
            if status in ("gone", "error") or (status == "covered" and native is None):
                msg = (f"error: element #{idx} target {verdict} — "
                       f"skipping click (no dispatch)")
                log(msg)
                return msg
            if through:
                log(f"target=#{idx} mode=click-through cover={verdict}",
                    tag="actions:click")
            # The click's own humanized move is the settle onto the center.
            vpx, vpy = probe["px"], probe["py"]
            _bank_box(elements, idx, probe.get("box"), vp)
            res = await dispatch_verified_click(platform, vpx, vpy, harvest=False,
                                                native_aria=native)
            if res["outcome"] == "error":
                msg = f"error clicking element #{idx}: {res['info']}"
                log(msg)
                return msg
            msg = (
                f"element #{idx} scroll+circle+click at "
                f"({vpx / vp['width']:.3f},{vpy / vp['height']:.3f}) humanize=true"
            )
            if native:
                msg += " via native locator (iframe-embedded)"
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
            log(f"target=#{idx} outcome={outcome} "
                f"x={vpx / vp['width']:.3f} y={vpy / vp['height']:.3f} "
                f"native={native is not None} (record in audit trail)",
                tag="actions:click")
            return msg
        except Exception as exc:  # noqa: BLE001
            msg = f"error clicking element #{idx}: {exc}"
            log(msg)
            return msg
        finally:
            if paused:
                platform.resume_idle_motion()


def _challenge_kind(el: ElementRef) -> str:
    """Classify a challenge control: slider / checkbox / captcha / none.

    Native role is authoritative: a checkbox is ALWAYS attempted via
    verified click — including "I'm not a robot" (a grid follow-up is a
    next-step problem, not a reason to refuse the toggle). Text markers
    only classify non-checkbox elements (image grids, puzzle links).
    """
    if el.kind == "checkbox":
        return "checkbox"
    if el.kind == "slider":
        return "slider"
    blob = f"{el.kind} {el.type} {el.label} {el.placeholder} {el.text} {el.id}".lower()
    if any(k in blob for k in ("captcha", "recaptcha", "puzzle", "verify you are human",
                               "i'm not a robot", "not a robot")):
        return "captcha"
    if "slider" in blob or "slide to" in blob or "drag" in blob:
        return "slider"
    if "checkbox" in blob:
        return "checkbox"
    return "none"


def _followup_idx(elements: list[ElementRef], default_idx: int) -> int:
    """Idx of the open image-grid follow-up element in the map.

    Matches the grid instruction node ("select all images ...",
    "verify you are human") so the OCR pipeline captures the popup,
    not the anchor checkbox. Falls back to the checkbox idx (whose
    capture then falls back to a direct bframe screenshot).
    """
    markers = ("select all images", "select each image",
               "verify you are human")
    for e in elements:
        blob = (f"{e.kind} {e.type} {e.label} {e.placeholder} "
                f"{e.text} {e.id}").lower()
        if any(k in blob for k in markers):
            return e.idx
    return default_idx


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
            # Show each stage: classify what is on screen right now
            # (provider + stage + signal) so the feed names the attempt
            # instead of burying it in probe verdicts.
            from .capability.stage import classify_stage, collect_metadata
            from .runner.policy import next_action

            try:
                _stage_text = await get_page_text(platform, limit=800)
            except Exception:  # noqa: BLE001
                _stage_text = ""
            try:
                _stage_title = await asyncio.wait_for(
                    page.title(), timeout=5.0)
            except Exception:  # noqa: BLE001
                _stage_title = ""
            _meta = await collect_metadata(page, _stage_text, _stage_title)
            _stg = classify_stage(_meta)
            _strat = next_action(_stg.stage, 0)
            log(f"provider={_stg.provider} stage={_stg.stage} "
                f"signal={_stg.get('signal') or 'no-signal'} "
                f"auto={int(_stg.auto_solvable)} "
                f"vision={int(_stg.needs_vision)} "
                f"strategy={_strat['action']}", tag="actions:stage")
            if kind == "captcha":
                # Image/text CAPTCHA: run the local ddddocr pipeline and
                # return the structured result for JEV review. No typing,
                # clicking, or dragging happens here — the next step's
                # normal propose/decide path acts on the suggestion.
                from .capability.captcha_ocr import (
                    pack_result_line,
                    solve_challenge,
                )

                try:
                    page_text = await get_page_text(platform, limit=800)
                except Exception:  # noqa: BLE001
                    page_text = ""
                ocr_res = await solve_challenge(
                    platform, elements, idx, page_text)
                if not ocr_res.available or ocr_res.error:
                    reason = ocr_res.error or "backend missing"
                    msg = (f"error: element #{idx} captcha/image challenge — "
                           f"escalating to done (ocr {reason})")
                    log(msg)
                    return msg
                msg = pack_result_line(idx, ocr_res)
                log(msg)
                return msg
            if kind == "none":
                msg = (f"error: element #{idx} is not a checkbox/slider "
                       f"(live kind {el.kind}) — skipping challenge (no dispatch)")
                log(msg)
                return msg
            # Probe WITHOUT dismissing first: for iframe-embedded
            # targets the "cover" is the iframe host itself, and an
            # Escape dismissal would close an open image follow-up
            # popup (seen live: popup opens, Escape closes it, checkbox
            # re-clicked forever, grid never captured). Only real
            # overlays on non-framed targets get the Escape treatment.
            probe = await probe_target(platform, elements, idx, expected_kind)
            # Framed targets (recaptcha checkboxes) sit behind an iframe
            # boundary elementFromPoint cannot pierce: dispatch natively via
            # the aria-ref locator instead of refusing. A stale point
            # verdict with an iframe cover verdict must not veto this
            # either — native dispatch is identity-based (aria ref) with
            # Playwright's own actionability checks, independent of the
            # coordinate probe that went stale (seen live: Google /sorry/
            # checkbox refused as "stale" with a covered:iframe point
            # verdict, never attempted). Resolver-level stale (no
            # frame_embedded key) stays a refusal.
            framed = (probe.get("frame_embedded") and bool(el.aria)
                      and probe["status"] in ("covered", "stale"))
            if framed:
                dismissed = False
            else:
                probe, dismissed = await verify_for_dispatch(
                    platform, elements, idx, expected_kind)
                framed = (probe.get("frame_embedded") and bool(el.aria)
                          and probe["status"] in ("covered", "stale"))
            # Open-grid short-circuit: when the image follow-up popup
            # is open, the anchor probe is MEANINGLESS (a div overlay
            # covers the checkbox — seen live as covered:div/stale) yet
            # the grid is fully solvable. Detect first, refuse later:
            # an open grid bypasses the probe refusal AND the Escape
            # dismiss (both would close what we're trying to capture).
            _followup_open = False
            if kind == "checkbox":
                from .capability.shield_solve import (
                    detect_image_followup as _detect_followup,
                )

                try:
                    _followup_text = await get_page_text(platform, limit=800)
                except Exception:  # noqa: BLE001
                    _followup_text = ""
                _followup_open, _followup_via = await _detect_followup(
                    platform, _followup_text)
                if _followup_open:
                    log(f"state=open via={_followup_via} "
                        f"probe_refusal=bypassed escape=skipped",
                        tag="actions:grid")
                    framed = framed or bool(el.aria)
                    dismissed = False
            if probe["status"] not in ("ok", "through") and not framed:
                if not _followup_open:
                    msg = (f"error: element #{idx} target {probe['verdict']} "
                           f"({probe['status']}) — "
                           f"skipping challenge (no dispatch)")
                    log(msg)
                    return msg
                log(f"state=open probe={probe['status']} decision=proceed",
                    tag="actions:grid")
            if kind == "checkbox":
                # Staged plan from runner.policy (pure order, mechanics
                # below): grid-capture alone when a follow-up is open,
                # else shield → toggle → paid. Each stage returns on
                # success; failures fall through to the next stage.
                from .capability.shield_solve import (
                    detect_image_followup,
                    detect_shield,
                )
                from .runner.policy import STEP_SOLVERS, plan_challenge

                try:
                    followup_text = await get_page_text(platform, limit=800)
                except Exception:  # noqa: BLE001
                    followup_text = ""
                followup, via = await detect_image_followup(
                    platform, followup_text)
                det = await detect_shield(platform, followup_text)
                family = (det.challenge_type
                          if det.detected else None)
                from .capability.twocaptcha_client import (
                    is_available as _tc_available,
                )
                from .capability.backends.kraken import (
                    is_available as _kraken_available,
                    new_session_id as _kraken_session,
                )
                _vision_ok = _kraken_available()
                from .trajectory.solver_ledger import (
                    score_for as _ledger_score,
                    stats as _ledger_stats,
                )

                # Effectiveness evidence derives from runs/*/steps.jsonl
                # (single source of truth) — no sidecar ledger to diverge.
                _stats = _ledger_stats()
                plan = plan_challenge(
                    kind="checkbox", framed=framed,
                    followup_open=followup, family=family,
                    paid_available=_tc_available(),
                    vision_available=_vision_ok, stats=_stats)
                from ._log_sink import kv as _kv

                _rates = {s: round(_ledger_score(
                    STEP_SOLVERS.get(s, s), _stats), 2) for s in plan}
                _kv("capability:actions:challenge_plan", target=f"#{idx}",
                    strategy=">".join(
                        f"{s}({_rates.get(s, '-')})" for s in plan))

                def _rank_tail() -> str:
                    import json as _json

                    return " solver-rank:" + _json.dumps(
                        {"order": list(plan), "rates": _rates},
                        ensure_ascii=False)
                _billing_session = _kraken_session() if _vision_ok else ""
                native = el.aria if framed else None
                # Machine tail for a shield attempt ("" when no family
                # solve ran): appended to toggle results so failed
                # token polls stay auditable without changing verdicts.
                shield_attempt_tail = ""
                toggle_info = ""
                for step in plan:
                    # Show each solver as it runs: stage name + attempt
                    # outcome lands in run.log AND the packed record logs.
                    _kv("capability:actions:challenge_stage",
                        target=f"#{idx}", stage=step)
                    if step == "captchakraken":
                        # Hosted vision grid solve (Kraken Abyss): tile
                        # discovery → numbered overlay → model rounds →
                        # tile clicks → verify gate. Falls through to
                        # ocr_grid audit capture when it can't clear.
                        from .capability.grid_solve import (
                            pack_result_line as _pack_grid,
                            run_vision_grid,
                        )
                        _grid_res = await run_vision_grid(
                            platform, instruction="",
                            session_id=_billing_session)
                        if _grid_res.success:
                            msg = _pack_grid(idx, _grid_res)
                            log(f"vision grid solved rounds={_grid_res.rounds} "
                                f"tiles={_grid_res.tiles_clicked} "
                                f"(record in audit trail)",
                                tag="actions:vision")
                            return msg + _rank_tail()
                        # Failed rounds must stay auditable (model picks,
                        # verify labels, per-round trail) — same pattern
                        # as shield-bypass-attempt: formatted JSON to console +
                        # run.log, then fall through to OCR capture.
                        from ._log_sink import emit_json as _emit_json

                        _emit_json({"event": "captchakraken-attempt",
                                    "record": _grid_res.to_record()})
                        log(f"unsolved error={_grid_res.error} "
                            f"fallback=capture", tag="actions:vision")
                        continue
                    if step == "ddddocr":
                        # Open image-grid follow-up: NEVER re-click the
                        # checkbox (that closes the popup) — route the
                        # grid to the OCR pipeline so the state machine
                        # captures it instead of destroying it.
                        from .capability.captcha_ocr import (
                            pack_result_line as _pack_ocr,
                            solve_challenge as _solve_grid,
                        )
                        fidx = _followup_idx(elements, idx)
                        ocr_res = await _solve_grid(
                            platform, elements, fidx, followup_text)
                        _ok = bool(ocr_res.available and not ocr_res.error)
                        if not _ok:
                            msg = (f"error: element #{idx} image follow-up open "
                                   f"({via}) but grid capture failed "
                                   f"({ocr_res.error or 'backend missing'}) — "
                                   f"leaving it open, no click dispatched")
                            log(msg)
                            return msg + _rank_tail()
                        log(f"state=open via={via} route=ocr_grid "
                            f"checkbox=untouched", tag="actions:grid")
                        msg = _pack_ocr(fidx, ocr_res)
                        return msg + _rank_tail()
                    if step == "shield-bypass":
                        # Native family click + token poll (shield_solve).
                        # Success returns a packed result for JEV review;
                        # failure keeps its error prefix for the failure
                        # rails while the attempt tail preserves the
                        # structured record for the audit trail.
                        from .capability.shield_solve import (
                            pack_attempt_tail,
                            pack_result_line,
                            solve_shield,
                        )
                        log(f"family={det.challenge_type} "
                            f"conf={det.confidence:.2f} target=#{idx} "
                            f"action=attempt", tag="actions:shield")
                        sol = await solve_shield(
                            platform, det.challenge_type, timeout_s=15.0)
                        if sol.success:
                            msg = pack_result_line(idx, sol)
                            log(f"family={det.challenge_type} "
                                f"solved token_len={sol.token_len} "
                                f"(record in audit trail)",
                                tag="actions:shield")
                            return msg + _rank_tail()
                        shield_attempt_tail = " " + pack_attempt_tail(sol)
                        log(f"family={det.challenge_type} "
                            f"error={sol.error} fallback=toggle",
                            tag="actions:shield")
                        continue
                    if step == "toggle":
                        if native:
                            log(f"target=#{idx} locator=native ref={native}",
                                tag="actions:toggle")
                        res = await dispatch_verified_click(
                            platform, probe["px"], probe["py"],
                            native_aria=native,
                            # Shield checkboxes animate: skip actionability
                            # waits (upstream shield-bypass clicks
                            # force=True), or the dispatch can hang past
                            # timeout without ever clicking.
                            force=native is not None)
                        if res["outcome"] == "error":
                            toggle_info = res["info"]
                            continue  # paid stage (when planned) is next
                        _bank_box(elements, idx, probe.get("box"),
                                  page.viewport_size or {"width": 1280, "height": 800})
                        msg = f"element #{idx} challenge checkbox toggled humanize=true"
                        if native:
                            msg += " via native locator (iframe-embedded)"
                        if dismissed:
                            msg += " + cover dismissed via Escape"
                        if res["outcome"] in ("newtab", "navigated") and res["info"]:
                            msg += f" + {res['outcome']} {res['info']}"
                        if res["snapshot"]:
                            msg += f" + {res['snapshot']}"
                        if shield_attempt_tail:
                            msg += shield_attempt_tail
                        log(f"target=#{idx} outcome={res['outcome']} "
                            f"native={native is not None} "
                            f"(record in audit trail)", tag="actions:toggle")
                        return msg
                    if step == "2captcha-python":
                        # Last resort: paid 2captcha solve (planned only
                        # when key + proxy are configured, so free runs
                        # never spend money and never hang on polling).
                        from .capability.twocaptcha_client import (
                            extract_sitekey,
                            pack_result_line as _tc_pack,
                            submit_recaptcha_token,
                        )
                        try:
                            _page_url = page.url
                        except Exception:  # noqa: BLE001
                            _page_url = ""
                        _sitekey = await extract_sitekey(page)
                        if _sitekey:
                            log(f"target=#{idx} backend=2captcha "
                                f"action=submit", tag="actions:paid")
                            _tc_res = await submit_recaptcha_token(
                                page, _sitekey, _page_url)
                            if _tc_res.success:
                                msg = _tc_pack(idx, _tc_res)
                                log(f"target=#{idx} solved "
                                    f"token_len={_tc_res.token_len} "
                                    f"(record in audit trail)",
                                    tag="actions:paid")
                                return msg
                            log(f"target=#{idx} error={_tc_res.error} "
                                f"fallback=escalate-toggle-error",
                                tag="actions:paid")
                        continue
                msg = (f"error toggling challenge checkbox #{idx}: "
                       f"{toggle_info}{shield_attempt_tail}")
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
            from .capability.human_move import mouse_move

            mouse = page.mouse
            await mouse_move(page, x0, cy)
            await mouse.down()
            try:
                for i in range(1, 9):
                    x = x0 + (x1 - x0) * i / 8
                    await mouse_move(page, x, cy)
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


async def execute_recovery(platform, strategy: str) -> str:
    """Dispatch one compensating action for a noop step; return a log line.

    refresh/back/escape only — the strategies select_recovery() may name.
    Settles like any action so the next SEE reads the result; the caller
    accounts acted/noops and audits the attempt. Fail-soft: platform
    failures become error: lines, never exceptions.
    """
    try:
        before_ids = platform.tab_ids()
    except Exception:  # noqa: BLE001
        before_ids = []
    try:
        prev_url = platform.page.url
    except Exception:  # noqa: BLE001
        prev_url = ""
    try:
        if strategy == "refresh":
            res = await platform.refresh_page()
        elif strategy == "back":
            res = await platform.go_back()
        elif strategy == "escape":
            res = await press_key(platform, "Escape")
        else:
            return f"error: unknown recovery {strategy}"
    except Exception as exc:  # noqa: BLE001
        return f"error: recovery {strategy} failed: {exc}"
    try:
        outcome, info = await platform.settle_after_action(prev_url, before_ids)
    except Exception:  # noqa: BLE001
        outcome, info = "same", ""
    snap = await _refresh_snapshot(platform)
    msg = f"recover:{strategy} — {res}"
    if outcome in ("newtab", "navigated") and info:
        msg += f" + {outcome} {info}"
    if snap:
        msg += f" + {snap}"
    log(msg)
    return msg


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
