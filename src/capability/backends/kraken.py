"""kraken.py — CaptchaKraken hosted vision backend (Abyss experts).

Library: https://github.com/JWriter20/CaptchaKraken
Upstream: https://github.com/JWriter20/CaptchaKraken (Source-Available
v1.1 — internal automation use is allowed; do NOT sell the solve or
ship it bundled as a product feature, so this backend is strictly
opt-in BYOK, never a default dependency).

Contract (verified against their docs + models.json + CLI behavior):
- OpenAI-compatible ``POST {VLLM_BASE_URL}/chat/completions`` with
  ``Authorization: Bearer $CAPTCHA_KRAKEN_API_KEY``.
- Hosted endpoint serves the routed Abyss mixture: name the expert on
  the wire — ``abyss-grid`` for tile grids (their served_aliases map;
  sending no model gets Twilight v1.2 prompts instead). Self-hosted
  endpoints resolve their own names, so the model is chosen by endpoint
  kind, never hardcoded blindly.
- Grid input MUST carry the numbered overlay (red labels, top-right,
  1-based cells) at a flat 518400 px area (720^2 serving band) —
  without it the same model scores ~zero on 4x4. See
  ``tiles.render_numbered_overlay``.
- Billing groups by ``X-CK-Session``: one value per captcha, cap 5
  billable responses per attempt. Image rounds cost 3 credits.
- Errors carry machine ``code`` (insufficient_credits,
  missing/invalid_api_key, rate_limited + retry_after_seconds,
  request_too_large, account_suspended, upstream_unavailable) —
  branch on code, never message text. Screenshots leave the machine
  on hosted (privacy note for operators).

This module is transport + parsing only (bytes in, 0-based tile
indexes out). The click/verify loop lives in ``grid_solve.py``.
"""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from typing import Any

import httpx

from ..logging_utils import log as _base_log


def log(msg: str, tag: str = "captchakraken") -> None:
    """Hierarchical domain logger (default ``captchakraken[:sub]``)."""
    _base_log(msg, tag=tag)

HOSTED_BASE_URL = "https://api.captchakraken.com/v1"
HOSTED_GRID_MODEL = "abyss-grid"
SELFHOST_GRID_MODEL = "captcha-v12"
PIXEL_BUDGET = 518400
REQUEST_TIMEOUT_S = 60.0
MAX_RESPONSE_CHARS = 4000


def _env() -> tuple[str, str]:
    """(api_key, base_url) — read at call time, after load_env."""
    return (os.environ.get("CAPTCHA_KRAKEN_API_KEY", "").strip(),
            os.environ.get("VLLM_BASE_URL", "").strip()
            or HOSTED_BASE_URL)


def _is_hosted(base_url: str) -> bool:
    return "captchakraken.com" in (base_url or "").lower()


def model_for_grid(base_url: str = "") -> str:
    """Expert name for grid requests on this endpoint.

    Hosted → ``abyss-grid`` (routed mixture arm). Anything else is an
    operator-run server resolving its own names → ``captcha-v12``
    served alias. Never empty.
    """
    _, base = _env()
    target = (base_url or base).rstrip("/")
    if _is_hosted(target):
        return HOSTED_GRID_MODEL
    return os.environ.get("CAPTCHA_LORA_NAME", "").strip() or SELFHOST_GRID_MODEL


def is_available() -> bool:
    """True when a key is configured (no network use)."""
    key, _ = _env()
    return bool(key)


def new_session_id() -> str:
    """One billing session per captcha (their 5-response cap groups here)."""
    return f"otc-{uuid.uuid4().hex[:16]}"


def grid_prompt(instruction: str, rows: int, cols: int) -> str:
    """Generation-2 grid prompt: numbered overlay + JSON array out."""
    count = max(1, rows * cols)
    task = " ".join(str(instruction or "").split())[:300]
    return (
        "Solve the captcha grid by choosing the cell numbers that match "
        f"the description from the captcha image prompt. Grid: {rows}x{cols} "
        f"({count} cells). Hint: separate images — select only clear matches. "
        + (f"Description: {task}. " if task else "") +
        "Cell numbers are drawn in red at the top-right of each cell, "
        "numbered 1 to {0}. Return JSON Array: [list of cell numbers].".format(count)
    )


def parse_indexes(text: Any, cell_count: int) -> list[int]:
    """Model reply → 0-based tile indexes (their reply is 1-based).

    Defensive: finds the first ``[...]`` run, accepts ints/floats,
    drops out-of-range/dupes, preserves reply order. [] on anything
    unparseable (never raise — caller treats as a failed round).
    """
    try:
        match = re.search(r"\[([^\[\]]*)\]", str(text or ""))
        if not match:
            return []
        out: list[int] = []
        for part in match.group(1).split(","):
            try:
                n = int(float(part.strip()))
            except (ValueError, TypeError):
                continue
            if 1 <= n <= max(1, cell_count) and (n - 1) not in out:
                out.append(n - 1)
        return out
    except Exception:  # noqa: BLE001
        return []


def _error_code(status: int, body: Any) -> str:
    """Machine error code from any failure shape (never message text)."""
    if status in (401, 403):
        return "invalid_api_key"
    if status == 429:
        return "rate_limited"
    if status == 413:
        return "request_too_large"
    if isinstance(body, dict):
        for key in ("code",):
            nested = body.get("ck_error") or body.get("error") or {}
            if isinstance(nested, dict) and nested.get(key):
                return str(nested[key])[:60]
            if isinstance(body.get(key), str) and key == "code":
                return str(body[key])[:60]
    if status >= 500:
        return "upstream_unavailable"
    return f"http_{status}"


async def solve_grid(overlay_png: bytes, instruction: str,
                     rows: int, cols: int, session_id: str = "",
                     timeout_s: float = REQUEST_TIMEOUT_S
                     ) -> tuple[list[int], dict[str, Any], list[str]]:
    """One vision round: overlay PNG → 0-based matching tile indexes.

    Returns ``(indexes, usage, logs)``. ``usage`` carries ``error`` +
    ``code`` on failure (codes per their contract). Never raises; never
    logs the key or image bytes.
    """
    logs = ["kraken grid round: "
            f"{rows}x{cols} session={(session_id or '')[:12]}"]
    key, base = _env()
    if not key:
        logs.append("unavailable: CAPTCHA_KRAKEN_API_KEY missing")
        return [], {"error": "CAPTCHA_KRAKEN_API_KEY missing",
                    "code": "missing_api_key"}, logs
    if not overlay_png:
        logs.append("refused: empty overlay image")
        return [], {"error": "empty overlay", "code": "bad_input"}, logs
    model = model_for_grid(base)
    image_b64 = base64.b64encode(overlay_png).decode("ascii")
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text",
                 "text": grid_prompt(instruction, rows, cols)},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
            ],
        }],
        "max_tokens": 100,
        "temperature": 0,
    }
    headers = {"Authorization": "Bearer <redacted>",
               "Content-Type": "application/json"}
    real_headers = {"Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "X-CK-Session": session_id or new_session_id()}
    logs.append(f"POST {base.rstrip('/')}/chat/completions model={model} "
                f"headers={headers}")
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(
                base.rstrip("/") + "/chat/completions",
                json=payload, headers=real_headers)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"transport failed ({exc.__class__.__name__})")
        return [], {"error": "transport failed",
                    "code": "transport_error"}, logs
    try:
        body = res.json()
    except ValueError:
        logs.append(f"non-JSON reply (http {res.status_code})")
        return [], {"error": "non-JSON reply",
                    "code": f"http_{res.status_code}"}, logs
    if res.status_code != 200 or not isinstance(body, dict):
        code = _error_code(res.status_code, body)
        logs.append(f"refused: {code} (http {res.status_code})")
        if code == "rate_limited":
            retry = 0
            try:
                nested = (body.get("ck_error") or {})
                retry = int(nested.get("retry_after_seconds") or 0)
            except (ValueError, TypeError, AttributeError):
                retry = 0
            return [], {"error": "rate-limited", "code": code,
                        "retry_after_seconds": retry}, logs
        return [], {"error": code, "code": code}, logs
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        logs.append("reply had no message content")
        return [], {"error": "empty reply", "code": "empty_reply"}, logs
    indexes = parse_indexes(
        str(text or "")[:MAX_RESPONSE_CHARS], rows * cols)
    usage: dict[str, Any] = {"model": model}
    try:
        usage["usage"] = body.get("usage", {})
    except AttributeError:
        pass
    logs.append(f"model returned {len(indexes)} tile(s): {indexes}")
    return indexes, usage, logs
