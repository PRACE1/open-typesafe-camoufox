"""
writer.py — the ONLY free-text generator (typesafe's writer.py equivalent).

The classifier (decide.py) never generates text. The writer runs in exactly
two narrow spots, each with a small packet and a structured reply:

  type_at  -> {fill, text}  (credential fields come back fill:false;
                             nothing is typed)
  goto/other -> {ok, url}   (code rejects anything but clean https URLs)

Passwords are never typed. Secrets ({ENV} placeholders) resolve at
execution in actions.py — values never enter prompts or logs.
Defaults to a small Groq model so no new key is required; ANTHROPIC_API_KEY
is an optional override.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass

import httpx

from .capability.jev_actions import _mask, _resolve_placeholders
from .decide import Kind
from .deps import ElementRef
from .perception import host_of

__all__ = [
    "WriterText", "WriterUrl", "ProposedAction",
    "compose_text", "propose_url", "propose_action",
    "validate_url", "_mask", "_resolve_placeholders",
]

DEFAULT_WRITER_MODEL = "llama-3.1-8b-instant"


@dataclass
class WriterText:
    fill: bool
    text: str


@dataclass
class WriterUrl:
    ok: bool
    url: str


def _writer_config() -> tuple[str, str, str]:
    """(base_url, api_key, model) — Groq default, Anthropic override."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            os.environ["ANTHROPIC_API_KEY"],
            os.environ.get("WRITER_MODEL", "claude-haiku-4-5"),
        )
    return (
        os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        os.environ.get("GROQ_API_KEY", ""),
        os.environ.get("WRITER_MODEL")
        or os.environ.get("GROQ_MODEL")
        or DEFAULT_WRITER_MODEL,
    )


async def _chat_json(system: str, user: str, timeout_s: float = 30.0) -> dict:
    base, key, model = _writer_config()
    if not key:
        return {}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        res = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {key}"},
        )
        res.raise_for_status()
        data = res.json()
    try:
        content = data["choices"][0]["message"]["content"]
        return json.loads(content) if isinstance(content, str) else {}
    except (KeyError, IndexError, ValueError, TypeError):
        return {}


def validate_url(url: str | None) -> str | None:
    """Return the cleaned URL iff it is a safe absolute https URL."""
    if not url or not isinstance(url, str):
        return None
    cleaned = url.strip().strip("\"'")
    try:
        parsed = urllib.parse.urlparse(cleaned)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return cleaned


async def compose_text(*, task: str, field_label: str, placeholder: str,
                       nearby_text: str, history: list[str],
                       is_credential: bool) -> WriterText:
    """Compose the string for a type_at. Credentials -> fill:false, no call."""
    if is_credential:
        return WriterText(fill=False, text="")
    user = (
        f"TASK: {task}\n"
        f"FIELD: {field_label!r} placeholder={placeholder!r}\n"
        f"NEARBY: {nearby_text[:300]}\n"
        f"HISTORY:\n" + "\n".join(history[-5:]) + "\n"
        'Reply JSON: {"fill": true/false, "text": "..."}. '
        "fill=false when no typing is needed (credentials, already-filled "
        "correct values, non-text fields)."
    )
    try:
        data = await _chat_json(
            "You fill one browser text field. Reply with JSON only.",
            user,
        )
    except Exception:
        return WriterText(fill=False, text="")
    text = data.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return WriterText(fill=False, text="")
    if len(text) > 500:
        text = text[:500]
    return WriterText(fill=bool(data.get("fill", True)), text=text)


async def propose_url(*, task: str, history: list[str]) -> WriterUrl:
    """Ask the writer for a URL when goto picks 'other'."""
    user = (
        f"TASK: {task}\nHISTORY:\n" + "\n".join(history[-5:]) + "\n"
        'Reply JSON: {"ok": true/false, "url": "https://..."}. '
        "ok=false when no navigation is needed."
    )
    try:
        data = await _chat_json(
            "You propose one navigation URL. Reply with JSON only.",
            user,
        )
    except Exception:
        return WriterUrl(ok=False, url="")
    cleaned = validate_url(data.get("url"))
    if not bool(data.get("ok", False)) or not cleaned:
        return WriterUrl(ok=False, url="")
    return WriterUrl(ok=True, url=cleaned)


PROPOSE_SYSTEM = (
    "You propose the single best next browser action. Reply with JSON only: "
    '{"question": "Should the browser ...?", "kind": "<verb>", '
    '"item": <element idx or null>, "url": "<absolute https URL or null>", '
    '"rationale": "<one sentence>"}. '
    "Verbs: wait, click_item, type_at, press_enter, press_escape, refresh, back, close_others, "
    "goto, done, none. click_item/type_at need a valid item idx from the map; "
    "goto needs an absolute https url or null; other verbs take item null "
    "and url null. click_item targets links/buttons only — never propose "
    "clicking an input/textarea/select (those are typed via type_at). "
    "Never propose done; completion is decided separately. "
    "Research tasks complete in the main content region — "
    "header/nav chrome rarely advances the task once results show. "
    "Never propose typing credentials; credential fields are handled separately."
)


@dataclass
class ProposedAction:
    """One LLM-proposed candidate + the yes/no question the Noul answers."""

    question: str
    kind: str
    item: int | None
    url: str | None
    rationale: str


def _element_line(e: ElementRef) -> str:
    label = e.label or e.placeholder or e.text or e.id or "?"
    bits = f"[{e.idx}] {e.kind} \"{label}\""
    if e.kind == "link" and e.href:
        host = host_of(e.href)
        if host:
            bits += f" -> {host}"
    if e.region:
        bits += f" [{e.region}]"
    if e.kind in ("input", "textarea", "select") or e.type:
        bits += " filled" if e.value_len else " empty"
    if e.href and not (e.kind == "link" and host_of(e.href)):
        bits += f" <{e.href[:60]}>"
    return bits


def summarize_elements(elements: list[ElementRef]) -> str:
    """Compact map grouped by kind so the proposer grounds idx to kind.

    Links, inputs, and buttons read as separate sections — a flat list lets
    the model attach a button's description to an input's idx (seen live).
    """
    groups: dict[str, list[str]] = {"link": [], "input": [], "button": []}
    other: list[str] = []
    for e in elements[:60]:
        line = _element_line(e)
        if e.kind in groups:
            groups[e.kind].append(line)
        elif e.kind in ("textarea", "select"):
            groups["input"].append(line)
        else:
            other.append(line)
    sections = []
    for name in ("link", "input", "button"):
        if groups[name]:
            sections.append(f"{name.upper()}S:\n" + "\n".join(groups[name]))
    if other:
        sections.append("OTHER:\n" + "\n".join(other))
    return "\n".join(sections)


async def propose_action(*, task: str, url: str, elements: list[ElementRef],
                         page_text: str, history: list[str],
                         notes: list[str], reading_note: str = "") -> ProposedAction | None:
    """Ask the small model for the single best next action as a yes/no question.

    Returns None when there is nothing to propose from (no key, no elements,
    model failure, or an invalid reply) — the caller then falls back to the
    Choice classification alone.
    """
    if not elements:
        return None
    context = ""
    if reading_note:
        context = f"CONTEXT: {reading_note}\n"
    user = (
        f"TASK: {task}\nURL: {url}\n{context}"
        f"ELEMENTS (idx kind label -> host [region] state):\n{summarize_elements(elements)}\n"
        f"PAGE TEXT:\n{(page_text or '').strip()[:600]}\n"
        f"NOTES:\n" + "\n".join(notes[-4:]) + "\n"
        f"HISTORY:\n" + "\n".join(history[-6:])
    )
    try:
        data = await _chat_json(PROPOSE_SYSTEM, user)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    kind = data.get("kind", "")
    if kind not in {k.value for k in Kind}:
        return None
    question = data.get("question", "")
    if not isinstance(question, str) or not question.strip() or len(question) > 300:
        return None
    item = data.get("item", None)
    if item is not None:
        try:
            item = int(item)
        except (ValueError, TypeError):
            return None
        if item < 0:
            return None
    raw_url = data.get("url", None)
    cleaned_url = validate_url(raw_url) if raw_url is not None else None
    if raw_url is not None and cleaned_url is None and kind == "goto":
        return None
    return ProposedAction(
        question=question.strip(),
        kind=kind,
        item=item,
        url=cleaned_url,
        rationale=str(data.get("rationale", ""))[:200],
    )
