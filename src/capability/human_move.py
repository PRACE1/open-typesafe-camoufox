"""
human_move: human path primitives for the browser cursor.

Speed model (measured on live Camoufox): each page.mouse.move() CDP
dispatch pays a humanize cost that scales with the browser's
humanize level (historical: 1.0 ~1.35s, 0.4 ~0.50s, 0.15 ~0.18s,
0.3 ~0.3-0.4s per dispatch).

WARNING (2026-09-18): browser-level humanize currently degrades per
dispatch at ANY level (measured: 2.1s -> 8.4s -> timeouts within 3
moves), wedging every mouse op until nothing dispatches. The runner
therefore defaults humanize OFF (see --humanize to opt back in); our
own multi-hop loops still draw human-like paths, and the per-dispatch
timeout guards below stay as the tripwire.

Path shape (independent of the browser flag): the HumanMoveMouse
statistical model (bundled PCA/GMM, 300 real human samples) picks lateral
waypoints; our multi-hop loops draw the curve point to point. The
pyautogui playback backend is never touched: quiesce, the JS cursor
tracker, and cursor.json all keep working.
"""

from __future__ import annotations

import asyncio
import math

import numpy as np

from .logging_utils import log

# Per-dispatch humanize cost at this level is ~0.3-0.4s (measured), sized
# to match Jev's ~150ms step loop so gestures stay around ~2s end-to-end.
# Tune here: lower = faster/straighter, higher = slower/looser.
HUMANIZE_LEVEL: float = 0.3

_model = None
_model_attempted = False


def _get_model():
    """Lazy singleton load of the bundled model (~1ms)."""
    global _model, _model_attempted
    if _model is not None:
        return _model
    if _model_attempted:
        return None
    _model_attempted = True
    try:
        from humanmouse.models import get_default_model_path
        from humanmouse.models.trajectory_model import HumanMouseModel

        _model = HumanMouseModel.load(get_default_model_path())
        log("[human-move] model loaded")
    except Exception as exc:  # noqa: BLE001 - fallback caller decides
        log(f"[human-move] model unavailable: {exc}")
        _model = None
    return _model


def subsample_arc(xy: np.ndarray, k: int) -> np.ndarray:
    """k hops: k+1 points at even arc-length spacing, endpoints always kept."""
    n = xy.shape[0]
    d = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    s = np.concatenate(([0.0], np.cumsum(d)))
    s = s / s[-1] if s[-1] > 0 else s
    fracs = np.linspace(0.0, 1.0, k + 1)
    idx = np.clip(np.searchsorted(s, fracs), 0, n - 1)
    return xy[idx]


def _move_with_timeout(page, x: float, y: float):
    return asyncio.wait_for(page.mouse.move(x, y), timeout=5.0)


async def _move_with_timeout_strict(page, x: float, y: float) -> None:
    """Like _move_with_timeout but surfaces a descriptive TimeoutError."""
    try:
        await asyncio.wait_for(page.mouse.move(x, y), timeout=5.0)
    except asyncio.TimeoutError:
        raise TimeoutError(f"mouse.move({x},{y}) timed out after 5s")


def _offset_sign(seed: int | None) -> float:
    """Deterministic per-seed side so the lateral offset is stable per seed."""
    if seed is None:
        return 1.0
    return 1.0 if seed % 2 == 0 else -1.0


async def human_move(
    page,
    start,
    end,
    *,
    k: int | None = None,
    vp: dict | None = None,
    seed: int | None = None,
) -> bool:
    """Human-like move from start to end.

    Short move (<150px, or k=0): one dispatch — the browser's humanize
    bezier is already a natural curve. Long move: two dispatches through
    one lateral mid waypoint (~55% along the chord, perpendicular offset
    ~10% of distance, side per seed) — the model's mid-arc sample when
    available, else a deterministic offset. A single humanize bezier
    between two points looks the same to the eye, so this is all the
    shape a move needs. Returns True on success.
    """
    (sx, sy), (ex, ey) = (float(start[0]), float(start[1])), (float(end[0]), float(end[1]))
    dist = math.hypot(ex - sx, ey - sy)
    if dist < 150 or (k is not None and k == 0):
        try:
            await _move_with_timeout_strict(page, ex, ey)
        except Exception as exc:  # noqa: BLE001 - same False contract as below
            log(f"[human-move] move failed: {exc}")
            return False
        return True

    mid = None
    model = _get_model()
    if model is not None:
        try:
            xy, _dt = await asyncio.to_thread(
                model.generate, (sx, sy), (ex, ey), 100, 0.3, seed
            )
            mid = tuple(map(float, subsample_arc(xy, 3)[2]))
        except Exception as exc:  # noqa: BLE001
            log(f"[human-move] generate failed: {exc}")
    if mid is None:
        ux, uy = (ex - sx) / dist, (ey - sy) / dist
        off = _offset_sign(seed) * 0.10 * dist
        mid = (sx + ux * dist * 0.55 - uy * off, sy + uy * dist * 0.55 + ux * off)

    try:
        await _move_with_timeout_strict(page, mid[0], mid[1])
        await _move_with_timeout_strict(page, ex, ey)
    except Exception as exc:  # noqa: BLE001
        log(f"[human-move] move failed: {exc}")
        return False
    return True


async def _replay(
    page,
    points: list[tuple[float, float]],
    vp_w: float,
    vp_h: float,
) -> None:
    """Replay target points through the in-browser cursor (no sleep: the
    browser-side humanize IS the timing)."""
    for x, y in points:
        x = max(1.0, min(float(x), vp_w - 1.0))
        y = max(1.0, min(float(y), vp_h - 1.0))
        await _move_with_timeout_strict(page, x, y)


async def human_loop(
    page,
    cx: float,
    cy: float,
    rx: float,
    ry: float,
    *,
    hops: int = 5,
    vp: dict | None = None,
    seed: int | None = None,
) -> tuple[float, float] | None:
    """One clean revolution around (cx, cy).

    hops evenly spaced points on a plain ellipse (no radius jitter, no
    extra wobble — the browser's per-dispatch bezier already renders each
    arc as a human curve, and double-stacking wobble was what read as
    inconsistent). Dispatches: start -> `hops` arc points; the following
    click's own humanized move closes the last gap and settles on the
    center. First dispatch doubles as the approach from wherever the
    cursor is. Returns the start point on success; None on failure.
    """
    hops = max(3, int(hops))
    t = np.linspace(0.0, 2 * np.pi, hops, endpoint=False)  # t[0] is 0.0 already
    pts = np.stack([cx + np.cos(t) * rx, cy + np.sin(t) * ry], axis=1)
    vp_w, vp_h = (vp["width"], vp["height"]) if vp else (1280, 800)
    try:
        await _replay(page, [tuple(map(float, p)) for p in pts], vp_w, vp_h)
    except Exception as exc:  # noqa: BLE001
        log(f"[human-loop] replay failed: {exc.__class__.__name__}: {exc}")
        return None
    return (float(pts[0, 0]), float(pts[0, 1]))