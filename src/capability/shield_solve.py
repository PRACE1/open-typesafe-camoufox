"""shield_solve.py — native anti-bot checkbox solver (shield-bypass port).

Technique source: https://github.com/genguzzz/shield-bypass (MIT),
verified via GitHub REST API + raw.githubusercontent fallbacks:
``bypass/plugins/cf_turnstile.py`` (iframe-scoped checkbox click at a
fixed widget offset, then token-input polling) and
``bypass/plugins/recaptcha.py`` (frame-locator anchor click, then
response-textarea polling). Investigation chain: API repo metadata →
contents listing → ``bypass/`` listing → ``plugins/`` listing →
``cf_turnstile.py`` + ``recaptcha.py`` + ``injector.py`` + ``SKILL.md``
+ ``reference.md`` + ``pyproject.toml`` raw sources.

Deliberate adaptation: upstream drives Patchright/Chromium; this
solver drives our Camoufox/Firefox page through stock async-Playwright
primitives (``frame_locator``, ``locator.click(delay=, force=)``,
``input_value`` polling) — no new dependency, no second browser
engine. ``patchright`` is intentionally kept OUT of pyproject.

Role in the solver: called from ``actions.challenge_control`` for
iframe-embedded checkbox challenges (Cloudflare Turnstile, Google
reCAPTCHA /sorry/, hCaptcha checkbox). Token *values* never enter
records or logs — only ``token_len`` (secrets hygiene, same convention
as credential masking). Every entry point is fail-soft: failures are
structured results, never exceptions, so the loop falls through to the
existing native-toggle / escalate rails.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from .logging_utils import log

TURNSTILE_IFRAME_SEL = (
    "iframe[src*='challenges.cloudflare.com'], "
    "iframe[src*='turnstile'], "
    "iframe[id*='cf-chl-widget']"
)
TURNSTILE_TOKEN_SEL = (
    "input[name='cf-turnstile-response'], "
    "textarea[name='cf-turnstile-response'], "
    "[id^='cf-chl-widget-']"
)
RECAPTCHA_IFRAME_SEL = (
    "iframe[src*='google.com/recaptcha'], "
    "iframe[src*='recaptcha.net']"
)
RECAPTCHA_TOKEN_SEL = "textarea[name='g-recaptcha-response']"
RECAPTCHA_ANCHOR_SEL = "#recaptcha-anchor, .recaptcha-checkbox-border"
HCAPTCHA_IFRAME_SEL = "iframe[src*='hcaptcha'], iframe[src*='hcaptcha.com']"
HCAPTCHA_TOKEN_SEL = "textarea[name='h-captcha-response']"

# Fixed widget click point inside a Turnstile iframe host, ported from
# upstream WIDGET_CLICK_X/Y (checkbox sits top-left of the widget).
WIDGET_CLICK = {"x": 26.0, "y": 32.0}
TOKEN_MIN_LEN = 20
POLL_INTERVAL_S = 0.5


class ShieldDetection(BaseModel):
    """One detected challenge family on the page."""

    model_config = {"extra": "ignore"}

    detected: bool = False
    challenge_type: str = "none"
    confidence: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)


class ShieldSolveResult(BaseModel):
    """Structured outcome of one shield-solve attempt.

    ``token_len`` proves a token materialized without ever recording
    the token value itself. Serialized into the step JSONL (``shield``
    block) and the per-step ``shield-NN.json`` audit file.
    """

    model_config = {"extra": "ignore"}

    available: bool = True
    challenge_type: str = "none"
    success: bool = False
    token_len: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """Compact dict for JSONL audit (no token values, ever)."""
        return {
            "available": self.available,
            "challenge_type": self.challenge_type,
            "success": self.success,
            "token_len": self.token_len,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "data": self.data,
        }


def is_available() -> bool:
    """True: the solver uses only stock async-Playwright primitives.

    Kept as a seam (mirrors captcha_ocr) so tests and future backends
    can gate on it without touching call sites.
    """
    return True


async def _locator_count(page, selector: str) -> int:
    """Fail-soft locator count (0 on anything unexpected)."""
    try:
        return int(await asyncio.wait_for(
            page.locator(selector).count(), timeout=5.0))
    except Exception:  # noqa: BLE001
        return 0


async def _read_token_len(page, selector: str) -> int:
    """Length of a token input's value, or 0 (never the value itself)."""
    try:
        loc = page.locator(selector).first
        if await asyncio.wait_for(loc.count(), timeout=5.0) == 0:
            return 0
        val = await asyncio.wait_for(loc.input_value(timeout=500),
                                     timeout=5.0)
        val = str(val or "")
        return len(val) if len(val) > TOKEN_MIN_LEN else 0
    except Exception:  # noqa: BLE001
        return 0


async def detect_shield(platform, page_text: str = "") -> ShieldDetection:
    """Detect the challenge family on the current page, best-first.

    Order mirrors upstream plugin priority: CF WAF markers are page
    text (cheap), then Turnstile / reCAPTCHA / hCaptcha iframe + token
    probes. Returns a non-detected result (never raises) when nothing
    matches, so callers fall through to existing rails.
    """
    page = platform.page
    text = (page_text or "").lower()
    if any(k in text for k in ("just a moment", "verifying you are human",
                               "challenges.cloudflare.com", "cf-chl")):
        return ShieldDetection(detected=True, challenge_type="cf_waf",
                               confidence=0.7,
                               details={"via": "page-text"})
    turnstile_frames = await _locator_count(page, TURNSTILE_IFRAME_SEL)
    turnstile_tokens = await _locator_count(page, TURNSTILE_TOKEN_SEL)
    if turnstile_frames > 0 or turnstile_tokens > 0:
        return ShieldDetection(
            detected=True, challenge_type="cf_turnstile", confidence=0.95,
            details={"frames": turnstile_frames, "tokens": turnstile_tokens})
    recaptcha_frames = await _locator_count(page, RECAPTCHA_IFRAME_SEL)
    recaptcha_tokens = await _locator_count(page, RECAPTCHA_TOKEN_SEL)
    if recaptcha_frames > 0 or recaptcha_tokens > 0:
        return ShieldDetection(
            detected=True, challenge_type="recaptcha", confidence=0.9,
            details={"frames": recaptcha_frames, "tokens": recaptcha_tokens})
    hcaptcha_frames = await _locator_count(page, HCAPTCHA_IFRAME_SEL)
    hcaptcha_tokens = await _locator_count(page, HCAPTCHA_TOKEN_SEL)
    if hcaptcha_frames > 0 or hcaptcha_tokens > 0:
        return ShieldDetection(
            detected=True, challenge_type="hcaptcha", confidence=0.85,
            details={"frames": hcaptcha_frames, "tokens": hcaptcha_tokens})
    return ShieldDetection()


async def _click_turnstile_checkbox(page) -> bool:
    """Port of upstream click_turnstile_checkbox: role checkbox first,
    iframe-host offset click as fallback. Returns True if a click
    dispatched (not proof of solve — token polling decides that)."""
    # Path 1: frame-locator role checkbox (most faithful click target).
    try:
        fl = page.frame_locator(TURNSTILE_IFRAME_SEL).first
        cb = fl.get_by_role("checkbox").first
        if await asyncio.wait_for(cb.is_visible(timeout=500),
                                  timeout=5.0):
            await asyncio.wait_for(
                cb.click(timeout=3000, force=True), timeout=8.0)
            return True
    except Exception as exc:  # noqa: BLE001
        log(f"[shield] turnstile role-click failed: {exc}")
    # Path 2: iframe host element-handle click at the fixed widget
    # offset with a human-like delay (upstream WIDGET_CLICK + delay=60).
    try:
        host = page.locator(TURNSTILE_IFRAME_SEL).first
        handle = await asyncio.wait_for(host.element_handle(timeout=500),
                                        timeout=5.0)
        if handle is None:
            return False
        await asyncio.wait_for(
            handle.click(position=dict(WIDGET_CLICK), timeout=3000,
                         delay=60, force=True),
            timeout=8.0)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"[shield] turnstile offset-click failed: {exc}")
        return False


async def _click_recaptcha_checkbox(page) -> bool:
    """Port of upstream RecaptchaPlugin.solve step 2: anchor click."""
    try:
        fl = page.frame_locator(RECAPTCHA_IFRAME_SEL).first
        anchor = fl.locator(RECAPTCHA_ANCHOR_SEL).first
        if await asyncio.wait_for(anchor.is_visible(timeout=2000),
                                  timeout=5.0):
            await asyncio.wait_for(
                anchor.click(timeout=3000), timeout=8.0)
            return True
        return False
    except Exception as exc:  # noqa: BLE001
        log(f"[shield] recaptcha anchor click failed: {exc}")
        return False


async def _poll_token(page, selector: str, deadline_s: float) -> int:
    """Poll a token input until a token-length value appears (len only)."""
    end = time.monotonic() + max(1.0, deadline_s)
    while time.monotonic() < end:
        n = await _read_token_len(page, selector)
        if n:
            return n
        await asyncio.sleep(POLL_INTERVAL_S)
    return 0


async def solve_shield(platform, challenge_type: str,
                       timeout_s: float = 15.0) -> ShieldSolveResult:
    """Attempt a native shield solve for one detected challenge family.

    Flow per family (ported from upstream solve()): token already
    present → immediate success; else dispatch the family checkbox
    click; then poll the token input to a deadline. Unknown families
    (cf_waf text-only, hcaptcha grids) return a structured failure so
    the caller falls through — never a blind click.

    The poll deadline is deliberately short (15s, not upstream's 35s):
    a working click materializes a token within seconds, and the loop
    re-attempts across steps — one long poll just burns the budget that
    route-around steps need.
    """
    t0 = time.monotonic()
    elapsed = lambda: int((time.monotonic() - t0) * 1000)
    page = platform.page
    if challenge_type == "cf_turnstile":
        n = await _read_token_len(page, TURNSTILE_TOKEN_SEL)
        if n:
            return ShieldSolveResult(challenge_type=challenge_type,
                                     success=True, token_len=n,
                                     elapsed_ms=elapsed(),
                                     data={"early_token": True})
        clicked = await _click_turnstile_checkbox(page)
        n = await _poll_token(page, TURNSTILE_TOKEN_SEL, timeout_s)
        if n:
            return ShieldSolveResult(challenge_type=challenge_type,
                                     success=True, token_len=n,
                                     elapsed_ms=elapsed(),
                                     data={"clicked": clicked})
        return ShieldSolveResult(
            challenge_type=challenge_type, success=False,
            elapsed_ms=elapsed(),
            error=(f"turnstile checkbox produced no token within "
                   f"{timeout_s}s timeout"),
            data={"clicked": clicked})
    if challenge_type == "recaptcha":
        n = await _read_token_len(page, RECAPTCHA_TOKEN_SEL)
        if n:
            return ShieldSolveResult(challenge_type=challenge_type,
                                     success=True, token_len=n,
                                     elapsed_ms=elapsed(),
                                     data={"early_token": True})
        clicked = await _click_recaptcha_checkbox(page)
        n = await _poll_token(page, RECAPTCHA_TOKEN_SEL, timeout_s)
        if n:
            return ShieldSolveResult(challenge_type=challenge_type,
                                     success=True, token_len=n,
                                     elapsed_ms=elapsed(),
                                     data={"clicked": clicked})
        return ShieldSolveResult(
            challenge_type=challenge_type, success=False,
            elapsed_ms=elapsed(),
            error=(f"recaptcha produced no token within {timeout_s}s "
                   f"(may require image challenge solve)"),
            data={"clicked": clicked})
    return ShieldSolveResult(
        challenge_type=challenge_type or "none", success=False,
        elapsed_ms=elapsed(),
        error=f"no native solver for challenge family "
              f"{challenge_type!r} (route to dddocr/escalate rails)")


RESULT_MARKER = "shield-solve:"


def pack_result_line(idx: int, result: ShieldSolveResult) -> str:
    """Single-line action result carrying the structured shield payload.

    Same machine-channel contract as captcha_ocr.pack_result_line: the
    JSON after the marker is parsed back out by the runner for the
    ``shield`` entry block, the ``shield-NN.json`` audit file, and the
    canonical steps.jsonl record. Token values are never embedded
    (lengths only).
    """
    import json as _json

    payload = _json.dumps(result.to_record(), ensure_ascii=False)
    verdict = "solved" if result.success else "unsolved"
    return (f"element #{idx} {RESULT_MARKER}{payload} "
            f"{verdict} token_len={result.token_len} "
            f"awaiting JEV review (no credential dispatch)")


def unpack_result_line(line: str) -> dict | None:
    """Extract the structured shield payload from a packed result line."""
    import json as _json

    try:
        start = line.index(RESULT_MARKER) + len(RESULT_MARKER)
    except ValueError:
        return None
    tail = line[start:].strip()
    depth = 0
    for i, ch in enumerate(tail):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = _json.loads(tail[:i + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


ATTEMPT_MARKER = "shield-attempt:"


def pack_attempt_tail(result: ShieldSolveResult) -> str:
    """Compact machine tail for FAILED shield attempts.

    Successes pack the full ``shield-solve:`` line (progress branch).
    Failures keep their ``error:`` prefix for the failure rails, with
    this tail appended so the attempt (family, token_len, elapsed,
    error) still lands in the audit trail instead of vanishing into
    prose. Same JSON schema as the success payload.
    """
    import json as _json

    return f"{ATTEMPT_MARKER}{_json.dumps(result.to_record(), ensure_ascii=False)}"


def unpack_attempt_tail(line: str) -> dict | None:
    """Extract a failed-attempt payload from a result-line tail."""
    import json as _json

    try:
        start = line.index(ATTEMPT_MARKER) + len(ATTEMPT_MARKER)
    except ValueError:
        return None
    tail = line[start:].strip()
    depth = 0
    for i, ch in enumerate(tail):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = _json.loads(tail[:i + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None
