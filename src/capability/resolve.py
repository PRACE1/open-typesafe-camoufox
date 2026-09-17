"""resolve.py — target resolution tiers (identity without mutation).

Pure lookup layer: native aria-ref identity, durable CSS selectors, and
positional nth-match, plus heal remap. No dispatches, no scrolling, no
logging — every failure collapses to a status dict the caller accounts.
Moved out of actions.py so dispatch orchestration stays readable.
"""

from __future__ import annotations

import asyncio

from .. import perception
from ..deps import ElementRef
from .aria_refs import ROLE_TO_KIND, resolve_ref
from .element_probe import RESOLVE_JS

TEXT_ENTRY_KINDS = frozenset({
    "input", "textarea", "select", "combobox", "searchbox", "spinbutton",
})


def _kinds_compatible(a: str, b: str) -> bool:
    """Same kind, or same text-entry widget under two vocabularies.

    The aria path reports role-mapped kinds (`input`) where the DOM probe
    reports tags (`textarea`); that granularity drift must never veto an
    action on its own.
    """
    return a == b or (a in TEXT_ENTRY_KINDS and b in TEXT_ENTRY_KINDS)


def _slot_changed(old: ElementRef, new: ElementRef) -> bool:
    """True when the nth probe slot now holds a different element.

    Incompatible kind change counts (see _kinds_compatible). Label/text
    change counts only when both sides have something to compare (empty
    labels carry no signal).
    """
    if not _kinds_compatible(old.kind, new.kind):
        return True
    o = (old.label or old.placeholder or old.text or "").strip().lower()
    n = (new.label or new.placeholder or new.text or "").strip().lower()
    return bool(o and n and o != n)


ARIA_IDENTITY_JS = """(el) => {
  const tag = (el.tagName || '').toLowerCase();
  const get = (a) => (el.getAttribute ? String(el.getAttribute(a) || '') : '');
  const text = (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
  const type = get('type');
  let kind = tag === 'a' ? 'link' : tag;
  if (tag === 'input' && type === 'checkbox') kind = 'checkbox';
  if (tag === 'input' && type === 'range') kind = 'slider';
  return {
    kind: kind,
    role: get('role'),
    label: (get('aria-label') || text || get('placeholder') || get('value') || '').slice(0, 80),
  };
}"""

SELF_HIT_JS = """(el, pt) => {
  let t = null;
  try { t = document.elementFromPoint(pt.x, pt.y); } catch (e) { return false; }
  if (!t) return false;
  try { return el === t || el.contains(t); } catch (e) { return false; }
}"""


def _labels_compatible(a: str, b: str) -> bool:
    """Lenient label match for the aria tier: node identity there comes
    from the native ref, so text drift (hydration, counters) must not veto.
    Missing labels carry no signal; containment either way counts."""
    x, y = (a or "").strip().lower(), (b or "").strip().lower()
    return not x or not y or x == y or x in y or y in x


async def _resolve_by_aria_ref(platform, aria: str) -> dict | None:
    """Native-identity lookup through the accessibility tree.

    Scrolls the node into view and returns {box(px dict), kind, label}.
    None when unresolvable — the caller falls through to the selector and
    nth-match tiers. Framed refs (fNeM) resolve at page level; Playwright
    routes them to their frame natively. Never writes to the DOM.
    """
    if not aria:
        return None
    locator = platform.page.locator(resolve_ref(aria, {aria}))
    try:
        if await locator.count() == 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        await asyncio.wait_for(
            locator.scroll_into_view_if_needed(timeout=5000), timeout=10.0)
        box = await locator.bounding_box()
    except Exception:  # noqa: BLE001
        return None
    if not box:
        return None
    try:
        ident = await asyncio.wait_for(
            locator.evaluate(ARIA_IDENTITY_JS), timeout=10.0)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(ident, dict):
        return None
    try:
        vp = platform.page.viewport_size or {"width": 1280, "height": 800}
        vw = max(1, int(vp.get("width", 1280)))
        vh = max(1, int(vp.get("height", 800)))
        if box["width"] > 1.5 * vw or box["height"] > 1.5 * vh:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    tag_kind = str(ident.get("kind", "") or "")
    role = str(ident.get("role", "") or "")
    # Role-first: composite widgets (textarea[role=combobox]) report their
    # semantic kind, not their DOM tag.
    kind = ROLE_TO_KIND.get(role, tag_kind)
    label = str(ident.get("label", "") or "")
    px_box = {"x": box["x"], "y": box["y"],
              "width": box["width"], "height": box["height"]}
    # Self-hit: the resolved node itself under its center means the point
    # check below cannot false-cover on vocabulary drift.
    cx = px_box["x"] + px_box["width"] / 2
    cy = px_box["y"] + px_box["height"] / 2
    try:
        self_hit = await asyncio.wait_for(
            locator.evaluate(SELF_HIT_JS, {"x": cx, "y": cy}),
            timeout=10.0)
    except Exception:  # noqa: BLE001
        self_hit = False
    return {"box": px_box, "kind": kind, "label": label,
            "self_hit": self_hit is True}


async def _resolve_by_selector(platform, sel: str) -> dict | None:
    """Read-only single-node lookup for a probe-generated selector.

    Returns {box(px dict), kind, label} or None when the node is gone,
    the selector is invalid, or the box is unusable. Never writes.
    """
    if not sel:
        return None
    try:
        hit = await asyncio.wait_for(
            platform.page.evaluate(RESOLVE_JS, sel), timeout=10.0)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(hit, dict):
        return None
    try:
        x, y, w, h = (float(v) for v in (hit.get("box") or []))
    except (ValueError, TypeError):
        return None
    if w <= 0 or h <= 0:
        return None
    vp = platform.page.viewport_size or {"width": 1280, "height": 800}
    vw = max(1, int(vp.get("width", 1280)))
    vh = max(1, int(vp.get("height", 800)))
    if w > 1.5 * vw or h > 1.5 * vh:
        return None
    kind = str(hit.get("kind", "") or "")
    label = (str(hit.get("label", "") or "")
             or str(hit.get("placeholder", "") or "")
             or str(hit.get("text", "") or "")
             or str(hit.get("id", "") or ""))
    return {"box": {"x": x, "y": y, "width": w, "height": h},
            "kind": kind, "label": label}


async def _confirm_aria_ref(platform, old: ElementRef) -> str:
    """Confirm a disputed aria ref against a fresh snapshot.

    Locator identity can phantom during hydration (a ref briefly resolving
    to a sibling while the anchor re-renders). The snapshot is the cheaper
    authority: same ref present with compatible kind/label means the node
    is alive and well — "ok". Present but incompatible — "stale". Absent —
    "unknown" (fall through to the selector/nth tiers).

    Returns "ok", "stale", or "unknown". Pure lookup.
    """
    try:
        fresh = await perception.find_elements(platform)
    except Exception:  # noqa: BLE001
        return "unknown"
    node = next((e for e in fresh if e.aria == old.aria), None)
    if node is None:
        return "unknown"
    same_kind = _kinds_compatible(old.kind, node.kind)
    same_label = _labels_compatible(
        old.label or old.placeholder or old.text or old.id or "",
        node.label or node.placeholder or node.text or node.id or "")
    if same_kind and same_label:
        return "ok"
    return "stale"


async def _resolve_target(platform, elements: list[ElementRef],
                          idx: int) -> dict:
    """Resolve positional idx to its live element across tiers.

    Tier order is priority order: native aria identity, durable selector,
    positional nth-match. Returns the FIRST ok verdict — acting on a
    verified-present node beats refusing on one tier's phantom verdict
    (seen live: an aria ref briefly resolving to a sibling heading while
    the anchor hydrated). Stale only when no tier resolves ok; gone when
    nothing resolves at all. The coordinate point check downstream stays
    the final misclick guard either way.

    Returns status ok (box, element, live_kind, live_label), stale (same
    slot or same selector, different element), or gone. Pure lookup — no
    dispatch, no scrolling.
    """
    blank = {"status": "gone", "box": None, "element": None,
             "live_kind": "", "live_label": ""}
    old = next((e for e in elements if e.idx == idx), None)
    if old is None:
        return blank
    stale_info: dict | None = None

    def _stale(info: dict) -> dict:
        nonlocal stale_info
        if stale_info is None:
            stale_info = {"status": "stale", **info}
        return stale_info

    # Tier 0 — native identity: the same a11y node regardless of DOM order.
    # Kind is strict up to vocabulary (role-mapped vs tag); labels are
    # lenient (node identity is already strong, hydration text drift must
    # not veto). A disagreeing locator is confirmed against a fresh
    # snapshot before it may refuse (hydration phantoms).
    if old.aria:
        hit = await _resolve_by_aria_ref(platform, old.aria)
        if hit is not None:
            info = {"box": hit["box"], "element": old,
                    "live_kind": hit["kind"], "live_label": hit["label"],
                    "self_hit": hit.get("self_hit", False)}
            if _kinds_compatible(old.kind, hit["kind"]) and _labels_compatible(
                    old.label or old.placeholder or old.text or old.id or "",
                    hit["label"]):
                return {"status": "ok", **info}
            confirmed = await _confirm_aria_ref(platform, old)
            if confirmed == "ok":
                return {"status": "ok", **info}
            if confirmed == "stale":
                return {"status": "stale", **info}
            _stale(info)
    if old.sel:
        hit = await _resolve_by_selector(platform, old.sel)
        if hit is not None:
            live = ElementRef(idx=idx, kind=hit["kind"], label=hit["label"])
            info = {"box": hit["box"], "element": old,
                    "live_kind": hit["kind"], "live_label": hit["label"]}
            if not _slot_changed(old, live):
                return {"status": "ok", **info}
            _stale(info)
    try:
        fresh = await perception.find_elements(platform)
    except Exception:  # noqa: BLE001
        return stale_info if stale_info is not None else blank
    if idx < 0 or idx >= len(fresh):
        return stale_info if stale_info is not None else blank
    cand = fresh[idx]
    box = None
    if cand.box is not None:
        vp = platform.page.viewport_size or {"width": 1280, "height": 800}
        vw = max(1, int(vp.get("width", 1280)))
        vh = max(1, int(vp.get("height", 800)))
        bx, by, bw, bh = cand.box
        # Unaimable spans (multi-viewport card anchors): no point on screen
        # can be verified as the target, so refuse rather than guess.
        if 0 < bw <= 1.5 and 0 < bh <= 1.5:
            box = {"x": bx * vw, "y": by * vh,
                   "width": bw * vw, "height": bh * vh}
    live_label = (cand.label or cand.placeholder or cand.text
                  or cand.id or "")
    info = {"box": box, "element": cand, "live_kind": cand.kind,
            "live_label": live_label}
    if box is None:
        return stale_info if stale_info is not None else {**blank, **info}
    if _slot_changed(old, cand):
        return _stale(info)
    return {"status": "ok", **info}


def _remap_idx(fresh: list[ElementRef], old_label: str,
               old_kind: str | None = None,
               old_sel: str | None = None,
               old_aria: str | None = None) -> int | None:
    """Find the old target in a fresh element map.

    Same a11y node first (aria-ref equality survives re-renders), then the
    durable selector, then exact label (case-insensitive). Label match
    stays strict on purpose — a wrong-element click is worse than a no-op.
    """
    if old_aria:
        for e in fresh:
            if e.aria == old_aria:
                return e.idx
    if old_sel:
        for e in fresh:
            if e.sel == old_sel:
                return e.idx
    want = (old_label or "").strip().lower()
    if not want:
        return None
    for e in fresh:
        lab = (e.label or e.placeholder or e.text or "").strip().lower()
        if lab == want and (old_kind is None or e.kind == old_kind):
            return e.idx
    return None


async def read_input_value(platform, aria: str) -> str:
    """Live DOM value of an input/combobox by its aria ref.

    Snapshot `: value` suffixes are unreliable (Google hides the typed
    query when suggestions render), so the retype guard reads the DOM
    property directly. Aria refs only — never positional fallbacks.
    Fail-soft "" on anything unexpected.
    """
    if not aria:
        return ""
    try:
        locator = platform.page.locator(resolve_ref(aria, {aria}))
        if await locator.count() == 0:
            return ""
        val = await asyncio.wait_for(
            locator.input_value(timeout=3000), timeout=5.0)
        return val or ""
    except Exception:  # noqa: BLE001
        return ""


async def heal_target(platform, old_label: str,
                      old_kind: str | None = None,
                      old_sel: str | None = None,
                      old_aria: str | None = None
                      ) -> tuple[list[ElementRef], int | None]:
    """One self-healing retry: re-probe the page, remap by aria/selector/label.

    Returns (fresh_elements, new_idx | None). The fresh list MUST be used
    for the re-attempt: positional refs and selectors belong to their own
    probe, and retrying against the stale step list re-resolves the wrong
    node. Pure lookup — no dispatch.
    """
    try:
        fresh = await perception.find_elements(platform)
    except Exception:  # noqa: BLE001
        return [], None
    return fresh, _remap_idx(fresh, old_label, old_kind, old_sel, old_aria)
