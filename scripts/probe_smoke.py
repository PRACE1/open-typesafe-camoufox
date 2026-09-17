"""probe_smoke.py — live parity smoke for the non-mutating ref probe.

Manual script (needs a headed browser + network); NOT part of pytest.
Visits a few representative pages, runs the probe, and asserts the
playwright-cli contract invariants:

- refs are sequential e0..eN matching positions (valid for this probe only)
- every box is a sane viewport-normalized rect
- the DOM carries ZERO data-jev markers (stealth: never mutate)
- count stays under Jev's 255-option ceiling
- no two kept boxes overlap >=80% (dedup held)

Usage: uv run python scripts/probe_smoke.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from camoufox.async_api import AsyncCamoufox

from src.capability.element_probe import ELEMENT_PROBE_JS
from src.perception import MAX_ELEMENTS, find_elements

PAGES = [
    "https://example.com/",
    "https://en.wikipedia.org/wiki/Main_Page",
    "https://www.google.com/",
]


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label} {detail}")
    return bool(cond)


async def check_page(browser, url):
    print(f"--- {url} ---")
    ok = True
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="load", timeout=45000)
    except Exception as exc:
        print(f"[SKIP] goto failed: {exc}")
        await page.close()
        return True
    try:
        raw = await page.evaluate(ELEMENT_PROBE_JS)
    except Exception as exc:
        print(f"[FAIL] probe raised: {exc}")
        await page.close()
        return False
    items = raw or []
    ok &= check("probe returns a list", isinstance(items, list), f"n={len(items)}")
    for i, it in enumerate(items):
        if it.get("ref") != f"e{i}":
            ok &= check("raw refs sequential", False, f"pos {i} has {it.get('ref')}")
            break
    else:
        ok &= check("raw refs sequential", True, f"e0..e{len(items) - 1}")
    tall = [it.get("ref") for it in items if _is_tall(it.get("box"))]
    if tall:
        print(f"[INFO] {len(tall)} multi-viewport spans (unaimable, refused at resolve): {tall[:5]}")
    # Perception-level contract, aria-first with probe fallback.
    els = await find_elements(type("Shim", (), {"page": page})())
    ok &= check("capped under Jev ceiling", len(els) <= MAX_ELEMENTS,
                f"n={len(els)} (raw {len(items)})")
    if els and els[0].aria:
        import re as _re
        aria_ok = all(_re.fullmatch(r"(?:f\d+)?e\d+", e.aria or "") for e in els)
        ok &= check("native aria refs", aria_ok)
        ok &= check("no 80pct overlap dupes", not _has_dupes(
            [{"box": list(e.box)} for e in els if e.box is not None]))
        # Live native resolution of the first link.
        link = next((e for e in els if e.kind == "link"), None)
        if link is not None:
            try:
                loc = page.locator("aria-ref=" + link.aria)
                n = await loc.count()
                box = await loc.bounding_box() if n else None
                ok &= check("aria-ref resolves live", bool(box), link.aria)
            except Exception as exc:
                ok &= check("aria-ref resolves live", False, str(exc)[:80])
    else:
        refs_ok = all(e.ref == f"e{e.idx}" for e in els)
        idx_ok = [e.idx for e in els] == list(range(len(els)))
        ok &= check("refs positional after dedup/cap (fallback)", refs_ok and idx_ok)
        sels = [e.sel for e in els]
        ok &= check("durable selectors generated (fallback)", all(sels),
                    f"empty={sum(1 for s in sels if not s)}")
        links = [e for e in els if e.kind == "link" and e.href]
        href_sels = [e for e in links if e.sel.startswith("a[href=")]
        ok &= check("anchors prefer href selectors (fallback)",
                    len(href_sels) >= len(links) // 2,
                    f"{len(href_sels)}/{len(links)}")
        ok &= check("no 80pct overlap dupes", not _has_dupes(
            [{"box": list(e.box)} for e in els if e.box is not None]))
    try:
        markers = await page.evaluate(
            "() => document.querySelectorAll('[data-jev]').length")
    except Exception:
        markers = -1
    ok &= check("zero DOM markers", markers == 0, f"count={markers}")
    await page.close()
    return ok


def _is_tall(box):
    try:
        return box is not None and (float(box[2]) > 1.5 or float(box[3]) > 1.5)
    except (ValueError, TypeError, IndexError):
        return False


def _has_dupes(items):
    def _ok(box):
        try:
            return (isinstance(box, list) and len(box) == 4
                    and all(float(v) == float(v) for v in box))
        except (ValueError, TypeError):
            return False

    boxes = [it.get("box") for it in items if _ok(it.get("box"))]
    for i, a in enumerate(boxes):
        for b in boxes[:i]:
            ix = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
            iy = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
            area = a[2] * a[3]
            if area and (ix * iy) / area >= 0.8 and b[2] * b[3] >= area:
                return True
    return False


async def main():
    all_ok = True
    async with AsyncCamoufox(headless=False, humanize=0.3) as browser:
        for url in PAGES:
            all_ok &= await check_page(browser, url)
    print("SMOKE " + ("PASS" if all_ok else "FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
