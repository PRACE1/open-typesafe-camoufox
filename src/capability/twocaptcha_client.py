"""twocaptcha_client.py — 2captcha paid-solve backend (optional).

Library: https://github.com/2captcha/2captcha-python
Upstream: https://github.com/2captcha/2captcha-python (``pip install
2captcha-python``), ``TwoCaptcha(apikey)`` with per-call polling knobs
(``defaultTimeout=120``, ``recaptchaTimeout=600``).

Role in the solver: LAST resort in ``challenge_control`` — only when the
native shield solve and the local ddddocr pipeline both fail AND the
operator configured a key + proxy. Submits sitekey + page URL (+ proxy,
forwarded to 2captcha's workers) and polls for a token. Token *values*
never enter records or logs — only ``token_len`` (same secrets hygiene
as ``shield_solve``).

Two proxies, one identity (2captcha's own Selenium examples do both):
1. API proxy — forwarded to 2captcha's workers so they solve from the
   same egress our browser uses (mismatched IPs tank the risk score).
2. Browser proxy — our Camoufox launch routes through the same
   residential endpoint (see ``browser_proxy()``).

Env (``.env.local``, git-ignored; read at call time like ``decide``):
  APIKEY_2CAPTCHA, TWOCAPTCHA_PROXY_TYPE/HOST/PORT/USER/PASS.

``twocaptcha`` is an optional import: missing lib or missing key yields
a structured ``available=False`` result instead of raising, so offline
runs and the test suite never need accounts, keys, or network.
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.parse
from typing import Any

from pydantic import BaseModel, Field

from .logging_utils import log as _base_log


def log(msg: str, tag: str = "2captcha-python") -> None:
    """Hierarchical domain logger (default ``2captcha-python[:sub]``).

    Named after the actual solve library:
    https://github.com/2captcha/2captcha-python
    """
    _base_log(msg, tag=tag)

BACKEND_NAME = "2captcha"
RESULT_MARKER = "2captcha-python:"


def _env() -> tuple[str, dict[str, str]]:
    """(api_key, proxy_fields) — read at call time, after load_env."""
    key = os.environ.get("APIKEY_2CAPTCHA", "").strip()
    proxy = {
        "type": os.environ.get("TWOCAPTCHA_PROXY_TYPE", "").strip(),
        "host": os.environ.get("TWOCAPTCHA_PROXY_HOST", "").strip(),
        "port": os.environ.get("TWOCAPTCHA_PROXY_PORT", "").strip(),
        "user": os.environ.get("TWOCAPTCHA_PROXY_USER", "").strip(),
        "pass": os.environ.get("TWOCAPTCHA_PROXY_PASS", "").strip(),
    }
    return key, proxy


def proxy_configured() -> bool:
    """True when a complete proxy identity is configured."""
    _, p = _env()
    return bool(p["host"] and p["port"] and p["user"] and p["pass"])


def proxy_for_api() -> dict[str, str] | None:
    """``{'type': ..., 'uri': 'user:pass@host:port'}`` for solve calls."""
    _, p = _env()
    if not proxy_configured():
        return None
    return {
        "type": (p["type"] or "HTTPS").upper(),
        "uri": f"{p['user']}:{p['pass']}@{p['host']}:{p['port']}",
    }


def masked_proxy() -> str:
    """Log-safe proxy identity: type + host + port, never credentials."""
    _, p = _env()
    if not p["host"]:
        return "(no proxy)"
    return f"{(p['type'] or 'HTTPS').upper()}://***@{p['host']}:{p['port']}"


def browser_proxy() -> dict[str, str] | None:
    """Playwright launch ``proxy=`` dict for the same egress.

    Camoufox forwards ``**launch_options`` to Playwright, so this dict
    drops straight into ``AsyncCamoufox(proxy=...)``. Server scheme
    follows the configured type (http/https/socks5).
    """
    _, p = _env()
    if not proxy_configured():
        return None
    scheme = (p["type"] or "http").lower()
    if scheme == "https":
        scheme = "http"  # Playwright: http(s) proxies share the scheme
    return {
        "server": f"{scheme}://{p['host']}:{p['port']}",
        "username": p["user"],
        "password": p["pass"],
    }


def launch_kwargs() -> dict[str, Any]:
    """Extra kwargs for ``AsyncCamoufox(...)``: the proxy when configured.

    One call per browser launch; logs the masked identity so runs show
    which egress they used without ever printing credentials.
    """
    prox = browser_proxy()
    if prox is None:
        return {}
    log(f"browser egress via {masked_proxy()}")
    return {"proxy": prox}


def check_proxy(timeout_s: float = 8.0) -> tuple[bool, str]:
    """TCP reachability of the configured proxy (no auth attempted).

    Returns (ok, detail) with no secrets. Preflight warns (never blocks)
    when unreachable — a dead proxy plus paid solves is money on fire.
    """
    _, p = _env()
    if not proxy_configured():
        return False, "proxy not configured in .env.local"
    import socket as _socket

    try:
        port = int(p["port"])
    except (ValueError, TypeError):
        return False, f"bad port {p['port']!r:.20}"
    try:
        with _socket.create_connection((p["host"], port),
                                       timeout=timeout_s):
            pass
        return True, f"{masked_proxy()} reachable"
    except Exception as exc:  # noqa: BLE001
        return False, f"{masked_proxy()} unreachable ({exc.__class__.__name__})"


def is_available() -> bool:
    """True when the lib imports AND an API key is configured."""
    key, _ = _env()
    if not key:
        return False
    try:
        import twocaptcha  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


class TwoCaptchaResult(BaseModel):
    """Structured outcome of one paid-solve attempt.

    Token values never recorded — ``token_len`` proves a token
    materialized. ``logs`` carries the solver trail inside the
    response (submitted ID, poll outcome), same contract as
    ``ShieldSolveResult``.
    """

    model_config = {"extra": "ignore"}

    available: bool = False
    captcha_type: str = "recaptcha"
    success: bool = False
    token_len: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        """Compact dict for JSONL audit (no token values, no secrets)."""
        return {
            "available": self.available,
            "captcha_type": self.captcha_type,
            "success": self.success,
            "token_len": self.token_len,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "data": self.data,
            "logs": list(self.logs),
        }


def sitekey_from_anchor_src(src: str) -> str:
    """Extract the ``k=`` sitekey from a recaptcha anchor iframe src."""
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(src or "").query)
        return (q.get("k") or [""])[0].strip()
    except (ValueError, AttributeError):
        return ""


async def extract_sitekey(page, timeout_s: float = 5.0) -> str:
    """Sitekey from the page: ``data-sitekey`` div first, anchor iframe
    ``k=`` param second. ``""`` when unresolvable (fail-soft)."""
    try:
        div = page.locator("div[data-sitekey]").first
        if await asyncio.wait_for(div.count(), timeout=timeout_s) == 0:
            raise RuntimeError("no data-sitekey div")
        key = await asyncio.wait_for(
            div.get_attribute("data-sitekey"), timeout=timeout_s)
        if (key or "").strip():
            return key.strip()
    except Exception:  # noqa: BLE001
        pass
    try:
        frame = page.locator(
            "iframe[src*='google.com/recaptcha'], "
            "iframe[src*='recaptcha.net']").first
        if await asyncio.wait_for(frame.count(), timeout=timeout_s) == 0:
            return ""
        src = await asyncio.wait_for(
            frame.get_attribute("src"), timeout=timeout_s)
        return sitekey_from_anchor_src(src or "")
    except Exception:  # noqa: BLE001
        return ""


async def solve_recaptcha(sitekey: str, url: str,
                          timeout_s: float = 120.0) -> TwoCaptchaResult:
    """Submit sitekey + page URL (+ configured proxy) and poll for a token.

    Paid call — the caller (``challenge_control``) only reaches here as
    a last resort. Never raises; every outcome is a structured result.
    The token value is never stored (length only).
    """
    t0 = time.monotonic()
    elapsed = lambda: int((time.monotonic() - t0) * 1000)
    logs = [f"2captcha submit: url={(url or '')[:80]} proxy={masked_proxy()}"]
    key, _ = _env()
    if not key:
        logs.append("unavailable: APIKEY_2CAPTCHA missing")
        return TwoCaptchaResult(logs=logs, elapsed_ms=elapsed(),
                                error="APIKEY_2CAPTCHA missing")
    try:
        from twocaptcha import TwoCaptcha
    except Exception as exc:  # noqa: BLE001
        logs.append(f"unavailable: import failed ({exc.__class__.__name__})")
        return TwoCaptchaResult(
            logs=logs, elapsed_ms=elapsed(),
            error=f"2captcha-python not installed: {exc}")
    if not (sitekey or "").strip():
        logs.append("refused: no sitekey (never guess)")
        return TwoCaptchaResult(
            available=True, logs=logs, elapsed_ms=elapsed(),
            error="no sitekey resolvable on page")
    proxy = proxy_for_api()
    if proxy is None:
        logs.append("refused: proxy not fully configured "
                    "(worker/browser IP mismatch burns paid solves)")
        return TwoCaptchaResult(
            available=True, logs=logs, elapsed_ms=elapsed(),
            error="2captcha proxy not fully configured in .env.local")

    token = await _submit_once(key, sitekey, url, proxy, timeout_s, logs)
    if not token:
        return TwoCaptchaResult(
            available=True, logs=logs, elapsed_ms=elapsed(),
            error=logs[-1] if logs else "2captcha solve failed")
    logs.append(f"solved (token_len={len(token)} elapsed_ms={elapsed()})")
    return TwoCaptchaResult(
        available=True, success=True, token_len=len(token),
        elapsed_ms=elapsed(), data={"proxy": masked_proxy()}, logs=logs)


async def _submit_once(key: str, sitekey: str, url: str,
                       proxy: dict[str, str], timeout_s: float,
                       logs: list[str]) -> str:
    """One paid submit → token string ("" on any failure, logged).

    The token is returned to the caller for immediate injection and
    must be discarded afterwards — it never enters a record or log.
    """
    from twocaptcha import TwoCaptcha

    def _run() -> dict[str, Any]:
        solver = TwoCaptcha(key, recaptchaTimeout=int(timeout_s))
        return solver.recaptcha(sitekey=sitekey, url=url, proxy=proxy)

    try:
        res = await asyncio.to_thread(_run)
    except Exception as exc:  # noqa: BLE001
        logs.append(f"solve raised ({exc.__class__.__name__})")
        log(f"solve failed: {exc.__class__.__name__}")
        return ""
    code = str((res or {}).get("code", "") or "")
    if not code:
        logs.append("solve returned no token")
    return code


async def submit_recaptcha_token(page, sitekey: str, url: str,
                                 timeout_s: float = 120.0
                                 ) -> TwoCaptchaResult:
    """Solve via 2captcha and inject the token into the page, one step.

    The token value lives only in a local: it is set into
    ``textarea[name='g-recaptcha-response']`` via evaluate and never
    stored, logged, or returned (only ``token_len`` leaves this
    function). Never raises.
    """
    t0 = time.monotonic()
    logs = [f"2captcha submit+inject: url={(url or '')[:80]} "
            f"proxy={masked_proxy()}"]
    key, _ = _env()
    if not key:
        logs.append("unavailable: APIKEY_2CAPTCHA missing")
        return TwoCaptchaResult(logs=logs,
                                elapsed_ms=int((time.monotonic() - t0) * 1000),
                                error="APIKEY_2CAPTCHA missing")
    proxy = proxy_for_api()
    if proxy is None:
        logs.append("refused: proxy not fully configured")
        return TwoCaptchaResult(
            available=True, logs=logs,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error="2captcha proxy not fully configured in .env.local")
    token = await _submit_once(key, sitekey, url, proxy, timeout_s, logs)
    if not token:
        return TwoCaptchaResult(
            available=True, logs=logs,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error=logs[-1] if logs else "2captcha solve failed")
    try:
        how = await asyncio.wait_for(
            page.evaluate(
                """(tok) => {
                    const el = document.querySelector(
                        "textarea[name='g-recaptcha-response']");
                    if (!el) return 'no-textarea';
                    el.value = tok;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    const cb = (window.___grecaptcha_cfg &&
                        window.___grecaptcha_cfg.clients &&
                        Object.values(window.___grecaptcha_cfg.clients)[0]);
                    const fn = cb && (cb.callback || cb['promise-callback']);
                    if (typeof fn === 'function') { fn(tok); return 'callback'; }
                    const form = el.closest('form');
                    if (form) { form.dispatchEvent(
                        new Event('submit', {bubbles: true, cancelable: true}));
                        return 'submit'; }
                    return 'value-set';
                }""",
                token,
            ),
            timeout=15.0,
        )
        how = str(how or "injected")
    except Exception as exc:  # noqa: BLE001
        logs.append(f"inject failed ({exc.__class__.__name__})")
        return TwoCaptchaResult(
            available=True, logs=logs,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            error="token solved but page injection failed")
    token_len = len(token)
    del token
    logs.append(f"token {how} (token_len={token_len}) — value discarded")
    return TwoCaptchaResult(
        available=True, success=True, token_len=token_len,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        data={"proxy": masked_proxy(), "injected": how}, logs=logs)


def pack_result_line(idx: int, result: TwoCaptchaResult) -> str:
    """Single-line action result carrying the structured paid-solve payload.

    Same machine-channel contract as ``shield_solve.pack_result_line``:
    the JSON after the marker parses back out for the audit block.
    Token values are never embedded (lengths only).
    """
    import json as _json

    payload = _json.dumps(result.to_record(), ensure_ascii=False)
    verdict = "solved" if result.success else "unsolved"
    return (f"element #{idx} {RESULT_MARKER}{payload} "
            f"{verdict} token_len={result.token_len} "
            f"awaiting JEV review (no credential dispatch)")


def unpack_result_line(line: str) -> dict | None:
    """Extract the structured paid-solve payload from a packed result line."""
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
