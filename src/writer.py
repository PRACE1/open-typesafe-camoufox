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
import uuid
from dataclasses import dataclass

import httpx

from .capability.jev_actions import _mask, _resolve_placeholders
from .decide import Kind, ref_to_idx
from .deps import ElementRef
from .perception import host_of

__all__ = [
    "WriterText", "WriterUrl", "ProposedAction",
    "compose_text", "propose_url", "propose_action", "synthesize_capability",
    "validate_url", "_mask", "_resolve_placeholders",
]

DEFAULT_WRITER_MODEL = "llama-3.1-8b-instant"

# Stable per-process session id (= per otc run): Go asks clients to send a
# stable x-opencode-session per conversation for routing/prompt caching.
_SESSION_ID = uuid.uuid4().hex
_WRITER_UA = "open-typesafe-camoufox/0.1"


def _writer_api_kind() -> str:
    """API shape: 'responses' (Muse Spark on Go), 'messages' (Anthropic
    Messages protocol, e.g. Union Alpha Free on Go), anything else means
    classic chat/completions."""
    raw = (os.environ.get("WRITER_API") or "chat").strip().lower()
    if raw in ("responses", "response", "responses-api", "openai-responses"):
        return "responses"
    if raw in ("messages", "message", "anthropic", "anthropic-messages"):
        return "messages"
    return "chat"


def _writer_headers(key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "User-Agent": _WRITER_UA,
        "x-opencode-session": _SESSION_ID,
    }


def _extract_responses_text(data: dict) -> str:
    """Pull assistant text out of an OpenAI Responses-API payload."""
    try:
        parts: list[str] = []
        for item in data.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                for chunk in item.get("content", []) or []:
                    if (isinstance(chunk, dict)
                            and chunk.get("type") == "output_text"
                            and chunk.get("text")):
                        parts.append(str(chunk["text"]))
        if parts:
            return "\n".join(parts)
        if isinstance(data.get("output_text"), str):
            return data["output_text"]
    except Exception:
        pass
    return ""


def _extract_messages_text(data: dict) -> str:
    """Pull assistant text out of an Anthropic Messages-API payload."""
    try:
        parts: list[str] = []
        for block in data.get("content", []) or []:
            if (isinstance(block, dict) and block.get("type") == "text"
                    and block.get("text")):
                parts.append(str(block["text"]))
        if parts:
            return "\n".join(parts)
    except Exception:
        pass
    return ""


@dataclass
class WriterText:
    fill: bool
    text: str


@dataclass
class WriterUrl:
    ok: bool
    url: str


def _writer_config() -> tuple[str, str, str]:
    """(base_url, api_key, model) — fully provider-driven from env.

    WRITER_* wins; falls back to GROQ_* (legacy default backend). Any
    OpenAI-compatible endpoint works: set WRITER_BASE_URL to point at it,
    WRITER_API_KEY for its key, WRITER_MODEL for the model id. The optional
    ANTHROPIC_* override (haiku-class writer) is preserved as-is.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return (
            os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            os.environ["ANTHROPIC_API_KEY"],
            os.environ.get("WRITER_MODEL", "claude-haiku-4-5"),
        )
    return (
        os.environ.get("WRITER_BASE_URL")
        or os.environ.get("GROQ_BASE_URL")
        or "https://api.groq.com/openai/v1",
        os.environ.get("WRITER_API_KEY") or os.environ.get("GROQ_API_KEY", ""),
        os.environ.get("WRITER_MODEL")
        or os.environ.get("GROQ_MODEL")
        or DEFAULT_WRITER_MODEL,
    )


async def _chat_json(system: str, user: str, timeout_s: float = 30.0,
                   max_tokens: int = 400) -> dict:
    base, key, model = _writer_config()
    if not key:
        return {}
    kind = _writer_api_kind()
    headers = _writer_headers(key)
    if kind == "responses":
        url = base.rstrip("/") + "/responses"
        payload: dict = {
            "model": model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_output_tokens": max_tokens,
        }
    elif kind == "messages":
        url = base.rstrip("/") + "/messages"
        headers = {**headers,
                   "anthropic-version": "2023-06-01",
                   "x-api-key": key}
        payload = {
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
    else:
        url = base.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        res = await client.post(url, json=payload, headers=headers)
        if res.status_code != 200:
            # Loud, key-safe provider diagnostics: a silent failure here
            # masquerades as "model declined" and stalls runs opaquely.
            # (Seen live: Groq 403 "Access denied" after heavy use.)
            from .capability.logging_utils import log as _log
            _log(f"writer model HTTP {res.status_code} (model={model}): "
                 f"{res.text[:160]}")
            res.raise_for_status()
        data = res.json()
    if kind == "responses":
        text = _extract_responses_text(data)
        try:
            return json.loads(text) if isinstance(text, str) and text else {}
        except ValueError:
            return {}
    if kind == "messages":
        text = _extract_messages_text(data)
        try:
            return json.loads(text) if isinstance(text, str) and text else {}
        except ValueError:
            return {}
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


@dataclass
class TaskVerdict:
    """Writer's judgment over the accumulated notes: done + note, or not."""

    done: bool
    note: str


async def summarize_task(*, task: str, notes: list[str]) -> TaskVerdict:
    """Judge from the pages read (notes) whether the task is accomplished.

    Called at most once per run, only when the loop is stalling with rich
    notes — the per-step Noul never flags completion, but the chat model can
    synthesize across pages. A non-empty note is required; anything else is
    a refusal and the run stops honestly.
    """
    excerpt = "\n".join(notes[-6:])[:1500]
    user = (
        f"TASK: {task}\nPAGES READ (url :: excerpt):\n{excerpt}\n"
        'Has the task been accomplished from these pages? Reply JSON: '
        '{"done": true/false, "note": "<outcome in 300 chars or empty>"}. '
        "done=true only when the task's concrete ask is answered above; "
        "never invent facts not present in the excerpts."
    )
    try:
        data = await _chat_json(
            "You judge task completion from page excerpts. Reply with JSON only.",
            user,
        )
    except Exception:
        return TaskVerdict(done=False, note="")
    if not isinstance(data, dict):
        return TaskVerdict(done=False, note="")
    note = data.get("note", "")
    if not isinstance(note, str) or not note.strip():
        return TaskVerdict(done=False, note="")
    if not bool(data.get("done", False)):
        return TaskVerdict(done=False, note="")
    return TaskVerdict(done=True, note=note.strip()[:300])


PROPOSE_SYSTEM = (
    "You propose the single best next browser action. Reply with JSON only: "
    '{"question": "Should the browser ...?", "kind": "<verb>", '
    '"item": <element ref like "e4"/"f3e7" or null>, "url": "<absolute https URL or null>", '
    '"rationale": "<one sentence>"}. '
    "Verbs: wait, click_item, type_at, press_enter, press_escape, refresh, back, close_others, "
    "goto, done, none. click_item/type_at need a valid item ref from the map; "
    "goto needs an absolute https url or null; other verbs take item null "
    "and url null. click_item targets links/buttons only — never propose "
    "clicking an input/textarea/select (those are typed via type_at). "
    "Never propose done; completion is decided separately. "
    "Research tasks complete in the main content region — "
    "header/nav chrome rarely advances the task once results show. "
    "Never propose typing credentials; credential fields are handled separately. "
    "Prefer pages not yet visited (their URLs appear in NOTES); revisit a page "
    "only to read what was missed. "
    "Never propose an element marked VISITED — that target was already read; "
    "proposing it again is always wrong, no matter how relevant it looks. "
    "If recent clicks were blocked by overlays, propose press_escape to dismiss "
    "them. If this page is read and offers nothing more, propose back to return "
    "to results (prefer back over goto-search)."
)


@dataclass
class ProposedAction:
    """One LLM-proposed candidate + the yes/no question the Noul answers."""
    question: str
    kind: str
    item: int | None
    url: str | None
    rationale: str


def _element_line(e: ElementRef, visited: bool = False,
                  mark: str = "") -> str:
    label = e.label or e.placeholder or e.text or e.id or "?"
    bits = f"[{e.ref}] {e.kind} \"{label}\""
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
    if mark:
        bits += f" {mark}"
    elif visited:
        bits += " VISITED — do not propose"
    return bits


def summarize_elements(elements: list[ElementRef], frontier=None) -> str:
    """Compact map grouped by kind so the proposer grounds ref to kind.

    Links, inputs, and buttons read as separate sections — a flat list lets
    the model attach a button's description to an input's ref (seen live).
    Capped to the probe map (already ≤128 by perception). When a frontier
    ledger is given, already-read targets are stamped VISITED so the
    proposer routes around them instead of re-picking the same result.
    """
    groups: dict[str, list[str]] = {"link": [], "input": [], "button": []}
    other: list[str] = []
    for e in elements[:128]:
        seen = False
        mark = ""
        if frontier is not None:
            try:
                mark = frontier.element_mark(e) or ""
                seen = bool(mark)
            except Exception:
                seen, mark = False, ""
        line = _element_line(e, visited=seen, mark=mark)
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
                         notes: list[str], reading_note: str = "",
                         frontier=None) -> ProposedAction | None:
    """Ask the small model for the single best next action as a yes/no question.

    Returns None when there is nothing to propose from (no key, no elements,
    model failure, or an invalid reply) — the caller then falls back to the
    Choice classification alone. ``frontier`` appends the to-do queue
    (pending labels) so proposals chase uncovered targets first.
    """
    if not elements:
        return None
    context = ""
    if reading_note:
        context = f"CONTEXT: {reading_note}\n"
    frontier_block = ""
    if frontier is not None:
        try:
            rec = frontier.to_record()
            pend = rec.get("pending_labels") or []
            frontier_block = (
                f"FRONTIER: {rec.get('visited', 0)} targets read, "
                f"{rec.get('pending', 0)} still to do. "
                f"Unvisited targets: {'; '.join(pend) if pend else '(none listed)'}.\n"
                "Propose an UNVISITED target whenever one serves the task.\n"
            )
        except Exception:
            frontier_block = ""
    user = (
        f"TASK: {task}\nURL: {url}\n{context}{frontier_block}"
        f"ELEMENTS (idx kind label -> host [region] state):\n{summarize_elements(elements, frontier)}\n"
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
        # Writer speaks refs (e4, f3e148); internal proposals stay ints.
        item = ref_to_idx(str(item), elements)
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


SYNTHESIZE_SYSTEM = (
    "You write ONE self-contained async Python browser capability. "
    "Reply with JSON only: {\"code\": \"<python source>\"}. "
    "Hard rules: exactly `async def execute(platform, ref, ctx, dry_run=False)` "
    "and nothing else at top level except `import asyncio` and constants. "
    "No imports beyond asyncio. No eval/exec/open/os/sys/subprocess. "
    "Drive ONLY via platform.page (Playwright async API: mouse, keyboard, "
    "evaluate, wait_for_timeout). ctx carries ref/role/label/box_norm "
    "(viewport-normalized x,y,w,h). With dry_run=True, resolve the target "
    "and return a status string WITHOUT dispatching anything. "
    "Return a one-line history string; prefix failures with 'error:'. "
    "Never invent helpers outside the function body."
)

_CAPABILITY_SKELETON = """\
import asyncio

async def execute(platform, ref, ctx, dry_run=False):
    page = platform.page
    box = ctx.get("box_norm") or []
    if dry_run:
        return f"{ref} dry-run ok"
    try:
        # TODO: implement the widget interaction here
        return f"{ref} acted"
    except Exception as exc:
        return f"error: healed action failed: {exc}"
"""


def _strip_fences(code: str) -> str:
    """Remove ```python fences models love to wrap code in."""
    text = (code or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


async def synthesize_capability(*, issue: str, ref: str, kind: str,
                                label: str, box_norm: list[float] | None,
                                page_excerpt: str,
                                timeout_s: float = 60.0) -> str | None:
    """Ask the writer to compose a capability for a novel widget failure.

    Returns Python source (validator-gated downstream) or None when the
    model declines, fails, or returns no usable code. Never raises.
    """
    box = ""
    try:
        if box_norm is not None:
            box = ",".join(f"{float(v):.3f}" for v in box_norm)
    except (ValueError, TypeError):
        box = ""
    user = (
        f"FAILURE: {(issue or '')[:300]}\n"
        f"TARGET: ref={ref} role={kind} label={(label or '')[:80]} "
        f"box_norm=[{box}]\n"
        f"PAGE: {(page_excerpt or '')[:400]}\n"
        f"SKELETON (adapt, keep the contract):\n{_CAPABILITY_SKELETON}"
    )
    try:
        data = await _chat_json(SYNTHESIZE_SYSTEM, user,
                                timeout_s=timeout_s, max_tokens=1500)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    code = _strip_fences(str(data.get("code", "") or ""))
    if "async def execute" not in code:
        return None
    return code
