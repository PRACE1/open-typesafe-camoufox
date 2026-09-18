"""verify_turnstile_live.py — headed end-to-end check for the screenX repair.

Manual script (needs a headed browser + network); NOT part of pytest.
Exercises the real path — ``detect_shield`` -> patch install ->
probe -> ``solve_shield`` — and prints ordered diagnostics so a
failure points at the right suspect:

- no challenge frames  -> detection/page issue (wrong URL?)
- probe FAIL            -> patch issue (frame targeting / evaluate)
- probe PASS + no token -> IP/reputation or poll window, not the click

Two modes (no third-party demo page needed)::

    uv run python scripts/verify_turnstile_live.py --local
    uv run python scripts/verify_turnstile_live.py <page-url> [--timeout 30]

``--local`` serves a self-contained page embedding Cloudflare's
documented dummy sitekeys (work from any domain incl. localhost):
the interactive key forces a real checkbox, so the probe reading —
not just the token — is the discriminating signal. Exit 0 only when
the probe passes AND a token materializes.
"""

import asyncio
import functools
import http.server
import os
import sys
import tempfile
import threading
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from camoufox.async_api import AsyncCamoufox

from src.capability.screenx_patch import (
    install_screenx_patch,
    probe_passes,
    probe_screenx,
)
from src.capability.shield_solve import detect_shield, solve_shield

# Cloudflare documented dummy keys (troubleshooting/testing docs).
# INTERACTIVE forces a real checkbox — the true test of the click path.
# EASY auto-passes and only exercises detection + token plumbing.
SITEKEY_INTERACTIVE = "3x00000000000000000000FF"
SITEKEY_EASY = "1x00000000000000000000AA"

LOCAL_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>turnstile check</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
</head><body>
<h1>turnstile check</h1>
<div class="cf-turnstile" data-sitekey="%SITEKEY%"></div>
</body></html>
"""


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label} {detail}", flush=True)
    return bool(cond)


def serve_local(sitekey):
    """Serve the dummy-key widget page on 127.0.0.1. Returns (server, url)."""
    tmp = tempfile.mkdtemp(prefix="turnstile_check_")
    with open(os.path.join(tmp, "check.html"), "w",
              encoding="utf-8") as fh:
        fh.write(LOCAL_PAGE.replace("%SITEKEY%", sitekey))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=tmp)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/check.html"


async def main(url, timeout_s):
    ok = True
    async with AsyncCamoufox(headless=False) as browser:
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=60000)
        except Exception as exc:
            print(f"[FAIL] goto failed: {exc}")
            return 1
        try:
            await page.wait_for_timeout(6000)
        except Exception:
            pass
        platform = types.SimpleNamespace(page=page)

        det = await detect_shield(platform)
        ok &= check("challenge detected", det.detected,
                    f"type={det.challenge_type} conf={det.confidence}")
        if not det.detected:
            await page.close()
            return 1

        n = await install_screenx_patch(page)
        ok &= check("patch installed in >=1 frame", n >= 1, f"n={n}")

        readings = await probe_screenx(page)
        ok &= check("probe readings present", len(readings) >= 1,
                    f"n={len(readings)}")
        for r in readings:
            small = (r.get("synthetic_small") or {})
            passed = probe_passes(r)
            ok &= check("probe screenX plausible",
                        passed, f"small={small.get('screenX')}")
            print(f"  frame={r.get('frame_url', '')[:80]}")

        sol = await solve_shield(platform, det.challenge_type,
                                 timeout_s=timeout_s)
        ok &= check("token materialized", sol.success,
                    f"token_len={sol.token_len} err={sol.error}")
        for line in sol.logs:
            print(f"  log: {line}")
        await page.close()
    print("VERIFY " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raw = list(sys.argv[1:])
    timeout = 30.0
    local = False
    easy = False
    rest = []
    skip_next = False
    for i, a in enumerate(raw):
        if skip_next:
            skip_next = False
            continue
        if a == "--local":
            local = True
        elif a == "--easy":
            easy = True
        elif a.startswith("--timeout"):
            if "=" in a:
                try:
                    timeout = float(a.split("=", 1)[1])
                except ValueError:
                    pass
            else:
                try:
                    timeout = float(raw[i + 1])
                    skip_next = True
                except (ValueError, IndexError):
                    pass
        elif a in ("-h", "--help"):
            print(__doc__)
            sys.exit(2)
        else:
            rest.append(a)
    server = None
    if local:
        if rest:
            print("ignoring URL arg with --local")
        key = SITEKEY_EASY if easy else SITEKEY_INTERACTIVE
        server, url = serve_local(key)
        print(f"local widget page: {url} "
              f"(key={'easy' if easy else 'interactive'})")
    elif not rest:
        print(__doc__)
        sys.exit(2)
    else:
        url = rest[0]
    try:
        code = asyncio.run(main(url, timeout))
    finally:
        if server is not None:
            server.shutdown()
    sys.exit(code)
