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

__all__ = [
    "WriterText", "WriterUrl", "compose_text", "propose_url",
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
