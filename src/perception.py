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
import re
import urllib.parse
from typing import Any
from urllib.parse import urljoin, urlparse

from .capability.element_probe import ELEMENT_PROBE_JS
from .deps import ElementRef, FocusedField

MAX_ELEMENTS = 128  # probe cap; well under Jev's 255-option Choice cap
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


_BLOCKED_MARKERS: tuple[tuple[str, str], ...] = (
    ("url", "/sorry/"),
    ("text", "unusual traffic"),
    ("text", "press & hold to confirm"),
    ("text", "verify you are human"),
    ("text", "verify you're not a robot"),
    ("text", "verify that you're not a robot"),
    ("text", "captcha"),
    ("text", "recaptcha"),
    ("text", "access denied"),
    ("text", "request blocked"),
)


def is_blocked_page(url: str, page_text: str) -> str | None:
    """Detect bot-check / block pages; return a short reason or None.

    Detection only — the loop keeps working such pages like any other
    (perceive → Jev classifies X/Y → humanized cursor acts → repeat).
    The reason rides in the state packet so Jev understands the context
    instead of treating it as an unloadable page.
    """
    lowered_url = (url or "").lower()
    for scope, marker in _BLOCKED_MARKERS:
        if scope == "url" and marker in lowered_url:
            return f"bot-check-by-url:{marker.strip('/')}"
    text = (page_text or "").lower()
    if not text.strip():
        return None
    for scope, marker in _BLOCKED_MARKERS:
        if scope == "text" and marker in text:
            return f"bot-check-by-text:{marker}"
    return None


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


_EXCERPT_STOPWORDS = frozenset({
    "about", "after", "before", "being", "could", "would", "should",
    "with", "from", "that", "this", "they", "them", "then", "than",
    "when", "where", "which", "while", "other", "these", "those",
    "into", "under", "between", "through", "during", "using", "make",
    "best", "most", "more", "such", "like", "what", "with",
})


def excerpt_for(task: str, page_text: str, width: int = 300) -> str:
    """Task-anchored excerpt: the window densest in task keywords.

    Page heads are usually nav chrome, and generic task words ("search",
    "article") hit inside that chrome first — so first-hit anchoring still
    banks junk. Density wins instead: the window containing the most
    DISTINCT task keywords is the meat (article body), chrome rarely holds
    more than one or two. Falls back to the head when nothing hits.
    """
    text = (page_text or "").strip().replace("\n", " ")
    if len(text) <= width:
        return text
    keywords = sorted({w.lower() for w in re.findall(r"[A-Za-z]{5,}", task or "")}
                      - _EXCERPT_STOPWORDS)
    if not keywords:
        return text[:width]
    lowered = text.lower()
    best_score, best_start = 0, 0
    step = max(25, width // 6)
    for start in range(0, len(text) - width + 1, step):
        window = lowered[start:start + width]
        score = sum(1 for kw in keywords if kw in window)
        if score > best_score or (
                score == best_score and score > 0
                and best_start < width <= start):
            # Tie-break away from the head: nav chrome lives in the first
            # window, so a tied later window is likelier to be article body.
            # A head window that strictly outscores everything still wins
            # (short pages whose content really is up front).
            best_score, best_start = score, start
    if best_score == 0:
        return text[:width]
    return text[best_start:best_start + width]

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
            box = _parse_box(raw.get("box"))
            sel = str(raw.get("durable", "") or "")[:200]
            out.append(ElementRef(
                idx=len(out),
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
                box=box,
                sel=sel,
                cx=float(raw.get("cx", 0.5)),
                cy=float(raw.get("cy", 0.5)),
            ))
        except (ValueError, TypeError):
            continue
    out = _dedup_overlaps(out)
    # Refs are positional and valid for this probe only: assign AFTER
    # dedup/cap so ref eN always addresses out[N]. (playwright-cli contract.)
    for i, e in enumerate(out):
        e.idx = i
        e.ref = f"e{i}"
    return out


def _parse_box(raw: object) -> tuple[float, float, float, float] | None:
    """Viewport-normalized rect (x, y, w, h in 0..1) or None when malformed."""
    try:
        x, y, w, h = (float(v) for v in (raw or []))  # type: ignore[union-attr]
    except (ValueError, TypeError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (x, y, w, h)


def _dedup_overlaps(elements: list[ElementRef]) -> list[ElementRef]:
    """Drop elements ≥80% contained in an earlier, larger box.

    Nested interactive nodes (a > button, input in label) double-report the
    same click target; keeping the larger (usually outer) box gives one ref
    per target and bounds Jev's context — our flat equivalent of
    playwright-cli's --depth trimming.
    """
    kept: list[ElementRef] = []
    for e in elements:
        if e.box is None:
            kept.append(e)
            continue
        x, y, w, h = e.box
        area = w * h
        drop = False
        for k in kept:
            if k.box is None:
                continue
            kx, ky, kw, kh = k.box
            ix = max(0, min(x + w, kx + kw) - max(x, kx))
            iy = max(0, min(y + h, ky + kh) - max(y, ky))
            inter = ix * iy
            if area and inter / area >= 0.8 and kw * kh >= area:
                drop = True
                break
        if not drop:
            kept.append(e)
    return kept


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
                lessons: str = "", blocked: str | None = None) -> dict[str, Any]:
    """Assemble the deterministic state packet sent to Jev."""
    return {
        "task": task,
        "url": url,
        "tabs": tabs,
        "lessons": lessons,
        "page_state": (
            f"blocked:{blocked} — interact with this page's verification "
            "controls (checkbox/button/input) like any page; classify "
            "positions, move, select, repeat until it resolves"
            if blocked else "normal"
        ),
        "notes": list(notes or [])[-6:],
        "visited": list(visited or [])[-10:],
        "elements": [
            {"ref": e.ref, "kind": e.kind, "type": e.type, "label": e.label,
             "placeholder": e.placeholder, "text": e.text, "cx": e.cx, "cy": e.cy,
             "value_len": e.value_len, "href": e.href,
            "region": e.region, "host": host_of(e.href)}
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
