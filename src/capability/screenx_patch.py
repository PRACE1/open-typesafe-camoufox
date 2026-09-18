"""screenx_patch.py — MouseEvent.screenX/Y repair for Turnstile iframes.

Port of upstream ``bypass/ext/script.js`` (genguzzz/shield-bypass, MIT)
into an evaluate-able snippet: no Chrome extension, no Patchright, no
second browser. The patch must run in the *challenge iframe's own
global* (cross-origin frames have separate ``MouseEvent.prototype``
chains — patching the top frame does nothing for the checkbox event),
so :func:`install_screenx_patch` evaluates it per Cloudflare frame
immediately before the click is dispatched. Prototype override applies
to subsequently created events, so document_start timing is NOT
required — presence at click time is.

Why this exists: Playwright synthetic clicks inside cross-origin
iframes surface ``screenX ~= clientX`` (small, < ~120); Cloudflare
discards those clicks and no token ever materializes. The override
rewrites implausible pairs to ``origin + client`` with a plausible
display origin. Genuine large coordinates pass through untouched.

Everything here is fail-soft: install/probe helpers never raise, so
``shield_solve`` proceeds exactly as before when patching is
impossible. Token values never appear — the probe reads back only
coordinates of synthetic probe events it constructs itself.
"""

from __future__ import annotations

import asyncio


# Faithful port of upstream ``bypass/ext/script.js``: same origin
# ranges (X 240-960 / Y 80-420), same needsPatch branches, same
# prototype override + marker attribute. Adapted only for
# evaluate-form: wrapped as an IIFE returning a status string.
SCREENX_PATCH_JS = """() => {
  if (globalThis.__cfTurnstileClickPatch) return 'already';
  globalThis.__cfTurnstileClickPatch = 1;

  const rand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
  const framed = (() => {
    try {
      return window.top !== window;
    } catch (_) {
      return true;
    }
  })();
  // Iframe window.screenX is often the widget offset (50-90). Using that
  // as origin makes patched screenX stay < 100 and Cloudflare still
  // rejects — same comment as upstream.
  const originX =
    !framed && typeof window.screenX === "number" && window.screenX > 50
      ? window.screenX
      : rand(240, 960);
  const originY =
    !framed && typeof window.screenY === "number" && window.screenY > 40
      ? window.screenY
      : rand(80, 420);

  function inIframe() {
    try {
      return window.top !== window;
    } catch (_) {
      return true;
    }
  }

  function clientOf(evt, axis) {
    if (axis === "X") return Number(evt.clientX || evt.x || 0) || 0;
    return Number(evt.clientY || evt.y || 0) || 0;
  }

  function needsPatch(native, client) {
    if (!Number.isFinite(native)) return true;
    // CDP Input.dispatchMouseEvent in a cross-origin iframe:
    // screenX === clientX (often < 120).
    if (native < 120 && Math.abs(native - client) < 2) return true;
    // Linux XTEST / Ozone: small screenX (widget offset ~50-90) not
    // equal to clientX, but Cloudflare's "screenX < 100" check still
    // discards it. macOS Quartz reports display coordinates
    // (hundreds) and must not be rewritten.
    if (inIframe() && native < 120) return true;
    return false;
  }

  function patchProto(proto) {
    if (!proto) return;
    for (const name of ["screenX", "screenY"]) {
      const desc = Object.getOwnPropertyDescriptor(proto, name);
      const origGet = desc && desc.get;
      const axis = name.endsWith("X") ? "X" : "Y";
      const origin = axis === "X" ? originX : originY;
      try {
        Object.defineProperty(proto, name, {
          configurable: true,
          enumerable: !!(desc && desc.enumerable),
          get() {
            let native = 0;
            try {
              native = origGet ? origGet.call(this) : 0;
            } catch (_) {}
            const client = clientOf(this, axis);
            if (needsPatch(native, client)) return origin + client;
            return native;
          },
        });
      } catch (_) {}
    }
  }

  patchProto(MouseEvent.prototype);
  if (typeof PointerEvent !== "undefined") patchProto(PointerEvent.prototype);
  try {
    document.documentElement.setAttribute("data-cf-ts-click", "1");
  } catch (_) {}
  return 'ok';
}"""

# Probe: constructs synthetic events (never touches the real checkbox)
# and reads back what the getter reports. ``synthetic_small`` mimics the
# CDP signature (screenX ~= small); ``genuine_large`` mimics a macOS
# Quartz event that must pass through untouched.
SCREENX_PROBE_JS = """() => {
  const mk = (sx, cx, sy, cy) => {
    const e = new MouseEvent('click', {
      screenX: sx, clientX: cx, screenY: sy, clientY: cy, bubbles: true,
    });
    return { screenX: e.screenX, screenY: e.screenY };
  };
  return {
    patched: typeof globalThis.__cfTurnstileClickPatch !== 'undefined',
    synthetic_small: mk(30, 50, 20, 40),
    genuine_large: mk(500, 480, 300, 290),
  };
}"""

# A patched ``synthetic_small`` must land at origin + client with the
# upstream origin ranges: X 240-960, Y 80-420.
PATCHED_MIN_X = 240
PATCHED_MAX_X = 960 + 1000
PATCHED_MIN_Y = 80
PATCHED_MAX_Y = 420 + 1000


def needs_patch(native: float, client: float, *, in_iframe: bool) -> bool:
    """Python mirror of the JS ``needsPatch`` rule (parity reference).

    Cloudflare discards clicks whose screen coords look synthetic;
    this is the exact predicate the override applies.
    """
    try:
        native_f = float(native)
    except (TypeError, ValueError):
        return True
    if native_f != native_f:  # NaN — not finite
        return True
    if native_f == float("inf") or native_f == float("-inf"):
        return True
    if native_f < 120 and abs(native_f - client) < 2:
        return True
    if in_iframe and native_f < 120:
        return True
    return False


def _is_challenge_frame_url(url: str) -> bool:
    url = (url or "").lower()
    return "challenges.cloudflare.com" in url or "turnstile" in url


async def install_screenx_patch(page, logs: list[str] | None = None) -> int:
    """Evaluate the patch in every Cloudflare challenge frame.

    Returns the patched frame count. Never raises — 0 means proceed
    exactly as before (fail-soft, mirrors shield_solve conventions).
    """
    trail = logs if logs is not None else []
    patched = 0
    try:
        frames = list(getattr(page, "frames", []) or [])
    except Exception:  # noqa: BLE001
        return 0
    for frame in frames:
        try:
            url = getattr(frame, "url", "") or ""
        except Exception:  # noqa: BLE001
            continue
        if not _is_challenge_frame_url(url):
            continue
        try:
            await asyncio.wait_for(frame.evaluate(SCREENX_PATCH_JS),
                                   timeout=5.0)
            patched += 1
        except Exception:  # noqa: BLE001
            continue
    trail.append(f"screenx: patch installed in {patched} frame(s)")
    return patched


async def probe_screenx(page, logs: list[str] | None = None) -> list[dict]:
    """Run the synthetic probe in every Cloudflare challenge frame.

    Returns per-frame readings (coordinates only — no tokens, no real
    input). Never raises.
    """
    trail = logs if logs is not None else []
    out: list[dict] = []
    try:
        frames = list(getattr(page, "frames", []) or [])
    except Exception:  # noqa: BLE001
        return out
    for frame in frames:
        try:
            url = getattr(frame, "url", "") or ""
        except Exception:  # noqa: BLE001
            continue
        if not _is_challenge_frame_url(url):
            continue
        try:
            reading = await asyncio.wait_for(
                frame.evaluate(SCREENX_PROBE_JS), timeout=5.0)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(reading, dict):
            out.append({"frame_url": url, **reading})
    trail.append(f"screenx: probed {len(out)} frame(s)")
    return out


def probe_passes(reading: dict) -> bool:
    """True when a probe reading shows the patch active and correct."""
    try:
        small = reading.get("synthetic_small") or {}
        large = reading.get("genuine_large") or {}
        sx, sy = float(small.get("screenX")), float(small.get("screenY"))
        lx, ly = float(large.get("screenX")), float(large.get("screenY"))
    except (TypeError, ValueError):
        return False
    if not (PATCHED_MIN_X <= sx <= PATCHED_MAX_X):
        return False
    if not (PATCHED_MIN_Y <= sy <= PATCHED_MAX_Y):
        return False
    # Genuine large coordinates must pass through untouched.
    return lx == 500 and ly == 300
