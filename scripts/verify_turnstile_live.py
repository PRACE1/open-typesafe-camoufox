"""verify_turnstile_live.py — headed end-to-end check for the screenX repair.

Manual script (needs a headed browser + network + a live Turnstile
widget); NOT part of pytest. Exercises the real path —
``detect_shield`` -> patch install -> probe -> ``solve_shield`` — and
prints ordered diagnostics so a failure points at the right suspect:

- no challenge frames  -> detection/page issue (wrong URL?)
- probe FAIL            -> patch issue (frame targeting / evaluate)
- probe PASS + no token -> IP/reputation or poll window, not the click

Usage: uv run python scripts/verify_turnstile_live.py <page-url> [--timeout 30]
Exit 0 only when the probe passes AND a token materializes.
"""

import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from camoufox.async_api import AsyncCamoufox

from src.capability.screenx_patch import (
    install_screenx_patch,
    probe_passes,
    probe_screenx,
)
from src.capability.shield_solve import detect_shield, solve_shield


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label} {detail}")
    return bool(cond)


async def main(url, timeout_s):
    ok = True
    async with AsyncCamoufox(headless=False) as browser:
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="load", timeout=60000)
        except Exception as exc:
            print(f"[FAIL] goto failed: {exc}")
            return 1
        await page.wait_for_timeout(4000)
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
    args = [a for a in sys.argv[1:] if not a.startswith("--timeout")]
    timeout = 30.0
    for i, a in enumerate(sys.argv[1:]):
        if a.startswith("--timeout"):
            try:
                timeout = float(a.split("=", 1)[1] if "=" in a
                                else sys.argv[1:][i + 1])
            except (ValueError, IndexError):
                pass
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(2)
    sys.exit(asyncio.run(main(args[0], timeout)))
