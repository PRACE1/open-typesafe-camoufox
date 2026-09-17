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
import hashlib
import urllib.parse
from typing import Any
from urllib.parse import urljoin, urlparse

from .capability.element_probe import ELEMENT_PROBE_JS
from .deps import ElementRef, FocusedField

MAX_ELEMENTS = 60  # probe cap; well under Jev's 255-option Choice cap
PAGE_TEXT_LIMIT = 1500
NOTES_LIMIT = 2000  # chars of extractive notes kept across the run

# Query params that identify the tracker, not the page — dropped when
# normalizing URLs for visited memory.
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "sca_esv", "sxsrf", "ei", "iflsig",
    "ved", "uact", "oq", "gs_lp", "sclient", "sei", "source", "gbv",
})


def norm_url(url: str) -> str:
    """Canonical identity for visited memory: lowercase host, no fragment,
    tracking params dropped, remaining query sorted. Keeps distinguishing
    params (e.g. Google's q) so different searches stay distinct."""
    try:
        p = urllib.parse.urlparse((url or "").strip())
    except ValueError:
        return (url or "")[:160]
    if not p.scheme or not p.netloc:
        return (url or "")[:160]
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
    q = sorted((k, v) for k, v in q if k.lower() not in _TRACKING_PARAMS)
    return urllib.parse.urlunparse((
        p.scheme.lower(), p.netloc.lower(), p.path or "/",
        "", urllib.parse.urlencode(q), "",
    ))


def host_of(href: str) -> str:
    """Destination host for criteria/state ('' when unknown or opaque)."""
    try:
        return urlparse(href or "").netloc.lower()
    except ValueError:
        return ""


def _unwrap_redirect(href: str) -> str:
    """Unwrap redirect wrappers (/url?q=<real>) to the destination URL.

    Narrow on purpose: only the well-known redirect path with an explicit
    http(s) q value. Opaque tokens (google.com/goto?url=...) stay as-is.
    """
    try:
        pr = urlparse(href)
    except ValueError:
        return href
    if pr.path != "/url":
        return href
    try:
        q = urllib.parse.parse_qs(pr.query).get("q", [""])[0]
    except ValueError:
        return href
    if q.startswith(("http://", "https://")):
        return q[:160]
    return href


def page_fingerprint(url: str, page_text: str) -> tuple[str, str]:
    """(normalized url, hash of leading text) — equal means the action had
    no observable effect on what the loop can read."""
    digest = hashlib.md5(((page_text or "")[:500].strip().encode("utf-8"))).hexdigest()
    return (norm_url(url), digest)


def trim_notes(notes: list[str], limit: int = NOTES_LIMIT) -> list[str]:
    """Drop oldest notes past the char budget (extractive long-horizon memory)."""
    notes = list(notes)
    while len(notes) > 1 and sum(len(n) for n in notes) > limit:
        notes.pop(0)
    return notes

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
    base_url = ""
    try:
        base_url = platform.page.url or ""
    except Exception:
        pass
    for raw in (result or [])[:MAX_ELEMENTS]:
        try:
            value = str(raw.get("value", "") or "")
            try:
                value_len = int(raw.get("value_len", 0) or 0)
            except (ValueError, TypeError):
                value_len = len(value)
            href = ""
            href_raw = str(raw.get("href", "") or "")
            if href_raw:
                try:
                    joined = urljoin(base_url, href_raw)
                    pr = urlparse(joined)
                    if pr.scheme in ("http", "https") and pr.netloc:
                        href = _unwrap_redirect(joined[:160])
                except ValueError:
                    href = ""
            region = str(raw.get("region", "") or "")[:24]
            out.append(ElementRef(
                idx=int(raw.get("idx", len(out))),
                kind=str(raw.get("kind", "?")),
                type=str(raw.get("type", "") or ""),
                id=str(raw.get("id", "") or ""),
                label=str(raw.get("label", "") or ""),
                placeholder=str(raw.get("placeholder", "") or ""),
                text=str(raw.get("text", "") or ""),
                value=value[:80],
                value_len=value_len,
                href=href,
                region=region,
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


def element_criteria(e: ElementRef, visited: set[str] | None = None) -> str:
    """One-line Choice criterion for an element idx.

    Inputs declare filled vs empty so the decider can reason about the
    clear-before-type system instead of retyping blindly. Links to
    already-visited pages are marked so multi-step exploration moves on.
    """
    label = e.label or e.placeholder or e.text or e.id or "?"
    extra = f" ({e.type})" if e.type else ""
    base = f"{e.kind}{extra} \"{label}\" at {e.cx:.3f},{e.cy:.3f}"
    if e.kind in ("input", "textarea", "select") or e.type:
        base += f" filled({e.value_len}ch)" if e.value_len else " empty"
    if e.region:
        base += f" [{e.region}]"
    host = host_of(e.href) if e.kind == "link" else ""
    if host:
        base += f" -> {host}"
    if visited and e.href and norm_url(e.href) in visited:
        base += " (visited)"
    return base


def build_state(*, task: str, url: str, elements: list[ElementRef],
                focused: FocusedField, page_text: str,
                history: list[str], frame: str = "", grid: str = "",
                tabs: int = 1, notes: list[str] | None = None,
                visited: list[str] | None = None,
                lessons: str = "") -> dict[str, Any]:
    """Assemble the deterministic state packet sent to Jev."""
    return {
        "task": task,
        "url": url,
        "tabs": tabs,
        "lessons": lessons,
        "notes": list(notes or [])[-6:],
        "visited": list(visited or [])[-10:],
        "elements": [
            {"idx": e.idx, "kind": e.kind, "type": e.type, "label": e.label,
             "placeholder": e.placeholder, "text": e.text, "cx": e.cx, "cy": e.cy,
             "value_len": e.value_len, "href": e.href,
             "region": e.region, "host": host_of(e.href),
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
