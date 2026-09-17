"""
perception.py — deterministic screen state (typesafe's perception.py equivalent).

Builds the structured state packet the Jev classifier decides over. The DOM
element map is our "numbered OCR blocks": inputs/buttons/links in reading
order with stable per-page-load idx tags, labels, and normalized centers.
Nothing here calls a model — capture, probe, merge, filter only.

Only depends on the platform adapter's page handle; never imports
playwright/camoufox directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .capability.element_probe import ELEMENT_PROBE_JS
from .deps import ElementRef, FocusedField

MAX_ELEMENTS = 40  # probe cap; well under Jev's 255-option Choice cap
PAGE_TEXT_LIMIT = 1500

FOCUSED_FIELD_JS = """
() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  const tag = (el.tagName || '').toLowerCase();
  const get = (n) => (el.getAttribute ? String(el.getAttribute(n) || '') : '');
  let label = get('aria-label');
  const id = el.id || '';
  if (!label && id) {
    const lbl = document.querySelector('label[for="' + id + '"]');
    if (lbl) label = (lbl.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  }
  const rawValue = (typeof el.value === 'string') ? el.value : (el.textContent || '');
  const r = (el.getBoundingClientRect) ? el.getBoundingClientRect() : null;
  return {
    role: get('role') || tag,
    label: (label || '').slice(0, 80),
    placeholder: get('placeholder').slice(0, 80),
    value: String(rawValue || '').slice(0, 200),
    input_type: get('type'),
    autocomplete: get('autocomplete'),
    name: (el.name ? String(el.name) : ''),
    id: String(id).slice(0, 80),
    frame: r ? {x: Math.round(r.left), y: Math.round(r.top),
                w: Math.round(r.width), h: Math.round(r.height)} : null,
  };
}
"""

_CREDENTIAL_HINTS = ("password", "passwd", "pwd", "passcode", "pin", "otp", "2fa")
_CREDENTIAL_TYPES = {"password"}
_CREDENTIAL_AUTOCOMPLETE = {"current-password", "new-password", "one-time-code"}


def is_credential(kind: str, input_type: str, label: str, placeholder: str,
                  name: str = "", autocomplete: str = "") -> bool:
    """True when the field looks like a credential (never free-typed)."""
    blob = f"{kind} {input_type} {label} {placeholder} {name}".lower()
    if (input_type or "").lower() in _CREDENTIAL_TYPES:
        return True
    if (autocomplete or "").lower() in _CREDENTIAL_AUTOCOMPLETE:
        return True
    return any(h in blob for h in _CREDENTIAL_HINTS)


def is_credential_element(e: ElementRef) -> bool:
    return is_credential(e.kind, e.type, e.label, e.placeholder)


async def find_elements(platform) -> list[ElementRef]:
    """Probe the live DOM; return the element map in reading order."""
    page = platform.page
    try:
        result = await asyncio.wait_for(
            page.evaluate(ELEMENT_PROBE_JS), timeout=10.0
        )
    except Exception:
        return []
    out: list[ElementRef] = []
    for raw in (result or [])[:MAX_ELEMENTS]:
        try:
            out.append(ElementRef(
                idx=int(raw.get("idx", len(out))),
                kind=str(raw.get("kind", "?")),
                type=str(raw.get("type", "") or ""),
                id=str(raw.get("id", "") or ""),
                label=str(raw.get("label", "") or ""),
                placeholder=str(raw.get("placeholder", "") or ""),
                text=str(raw.get("text", "") or ""),
                cx=float(raw.get("cx", 0.5)),
                cy=float(raw.get("cy", 0.5)),
            ))
        except (ValueError, TypeError):
            continue
    return out


async def get_page_text(platform, limit: int = PAGE_TEXT_LIMIT) -> str:
    """Visible body text — ground truth for reading results."""
    try:
        text = await asyncio.wait_for(
            platform.page.evaluate("() => document.body ? document.body.innerText : ''"),
            timeout=10.0,
        )
        return (text or "")[:limit]
    except Exception:
        return ""


async def get_focused_field(platform) -> FocusedField:
    """Describe document.activeElement (role/label/placeholder/value)."""
    try:
        raw = await asyncio.wait_for(
            platform.page.evaluate(FOCUSED_FIELD_JS), timeout=10.0
        )
    except Exception:
        raw = None
    if not raw:
        return FocusedField()
    kind = str(raw.get("role", "") or "")
    input_type = str(raw.get("input_type", "") or "")
    label = str(raw.get("label", "") or "")
    placeholder = str(raw.get("placeholder", "") or "")
    frame = raw.get("frame") or {}
    try:
        frame = {k: int(frame.get(k, 0)) for k in ("x", "y", "w", "h")}
    except (ValueError, TypeError, AttributeError):
        frame = {}
    return FocusedField(
        role=kind,
        label=label,
        placeholder=placeholder,
        value=str(raw.get("value", "") or ""),
        input_type=input_type,
        is_credential=is_credential(
            kind, input_type, label, placeholder,
            str(raw.get("name", "") or ""), str(raw.get("autocomplete", "") or ""),
        ),
        frame=frame,
    )


def format_elements(elements: list[ElementRef]) -> str:
    """Render the element map for prompts and payload dumps."""
    if not elements:
        return "(no actionable elements found on this page)"
    lines = []
    for e in elements:
        label = e.label or e.placeholder or e.text or e.id or "?"
        extra = f" ({e.type})" if e.type else ""
        lines.append(f"[{e.idx}] {e.kind}{extra} \"{label}\" @ {e.cx:.3f},{e.cy:.3f}")
    return "\n".join(lines)


def element_criteria(e: ElementRef) -> str:
    """One-line Choice criterion for an element idx."""
    label = e.label or e.placeholder or e.text or e.id or "?"
    extra = f" ({e.type})" if e.type else ""
    return f"{e.kind}{extra} \"{label}\" at {e.cx:.3f},{e.cy:.3f}"


def build_state(*, task: str, url: str, elements: list[ElementRef],
                focused: FocusedField, page_text: str,
                history: list[str], frame: str = "", grid: str = "",
                tabs: int = 1) -> dict[str, Any]:
    """Assemble the deterministic state packet sent to Jev."""
    return {
        "task": task,
        "url": url,
        "tabs": tabs,
        "elements": [
            {"idx": e.idx, "kind": e.kind, "type": e.type, "label": e.label,
             "placeholder": e.placeholder, "text": e.text, "cx": e.cx, "cy": e.cy,
             "sel": f'[data-jev="{e.idx}"]'}
            for e in elements
        ],
        "focused_field": {
            "role": focused.role, "label": focused.label,
            "placeholder": focused.placeholder,
            "value_len": len(focused.value),
            "is_credential": focused.is_credential,
        },
        "page_text": (page_text or "")[:800],
        "history": list(history[-8:]),
        "frame": frame,
        "grid": grid,
    }
