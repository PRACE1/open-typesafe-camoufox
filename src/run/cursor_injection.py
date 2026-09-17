"""
Cursor injection: synthesize cursor.json from the JS tracker buffer.

Cap records the screen WITHOUT the OS cursor, then renders a cursor from
cursor.json during export. That metadata normally comes from OS-level
GetCursorInfo polling, which never sees Camoufox's DOM-level mousemove
events — so this module harvests the JS tracker buffer and writes
cursor.json itself, patching the surrounding .cap project files to match.
"""

from __future__ import annotations

import json
import os
import shutil

from logging_utils import log

# ---------------------------------------------------------------------------
# Cursor injection: synthesize cursor.json from the JS tracker buffer
# ---------------------------------------------------------------------------

# Cap records the screen WITHOUT the OS cursor, then renders a cursor from
# cursor.json during export. The metadata normally comes from OS-level
# GetCursorInfo polling — which never sees Camoufox's DOM-level mousemove
# events. So we harvest our JS tracker buffer and write cursor.json ourselves.
#
# CursorMoveEvent schema (from crates/project/src/cursor.rs):
#   { active_modifiers: [], cursor_id: "0", time_ms: f64, x: f64, y: f64 }
#   x/y are NORMALIZED to screen space (0.0-1.0), not viewport pixels.
#
# CursorClickEvent schema:
#   { active_modifiers: [], cursor_num: 0, cursor_id: "0", time_ms: f64, down: bool }
#
# CASING: the installed Cap build deserializes the SNAKE_CASE names above, not
# the camelCase that serde rename_all = "camelCase" would imply. Writing only
# camelCase makes `cap export` fail with `missing field active_modifiers` and
# render no cursor. inject_from_harvest writes both casings per event; Cap
# ignores unknown fields, so whichever it wants is present.

# Screen resolution for normalization. Cap records the full screen; the
# browser viewport (1280x800) sits within it. We normalize the browser's
# clientX/clientY to the full screen's 0.0-1.0 range. For accuracy we'd need
# the actual screen size + window position, but 1920x1080 is the common
# default and Cap's renderer clamps/handles out-of-range coords gracefully.
DEFAULT_SCREEN_W = 1920
DEFAULT_SCREEN_H = 1080

# Cursor image metadata for the arrow cursor (the standard pointer).
# From Cap's crates/cursor-info/src/windows.rs: Arrow hotspot = (0.288, 0.189).
ARROW_HOTSPOT = {"x": 0.288, "y": 0.189}
ARROW_SHAPE = "Windows|Arrow"


def _make_normalizer(
    *,
    screen_w: int,
    screen_h: int,
    viewport_w: int,
    viewport_h: int,
    window_rect: tuple[int, int, int, int] | None,
):
    """Build a (client_x, client_y) -> (norm_x, norm_y) closure.

    The tracker captures clientX/clientY in the browser's viewport coordinate
    system (CSS pixels, origin top-left of the viewport). Cap's cursor.json
    wants coords NORMALIZED to the recorded screen (0.0-1.0, origin top-left of
    the screen). With --screen recording, that screen is the full desktop.

    The browser window sits inside the screen at an offset with its own size
    (title bar + chrome + the viewport). window_rect is the window's screen-
    space rect (left, top, right, bottom) from win32gui.GetWindowRect. The
    viewport is smaller than the window (chrome eats the top/sides), but using
    the window rect + assuming the viewport fills the client area is a far
    better approximation than the old divide-by-screen-only math, which placed
    a viewport-(640,400) cursor at screen-(0.333,0.370) regardless of where the
    browser actually was.

    Without a window_rect (non-Windows, or HWND not found), fall back to the
    old behavior (divide by screen dims) so the function still works — just
    misaligned, as before, rather than crashing.
    """
    if window_rect is None:
        def _fallback(client_x: float, client_y: float) -> tuple[float, float]:
            x = max(0.0, min(1.0, client_x / screen_w))
            y = max(0.0, min(1.0, client_y / screen_h))
            return x, y
        return _fallback

    win_left, win_top, win_right, win_bottom = window_rect
    win_w = max(1, win_right - win_left)
    win_h = max(1, win_bottom - win_top)

    def _mapped(client_x: float, client_y: float) -> tuple[float, float]:
        # viewport (0..viewport_w) maps linearly onto the window's client area
        # (win_left..win_right on screen), then we normalize to the screen.
        sx = win_left + (client_x / viewport_w) * win_w
        sy = win_top + (client_y / viewport_h) * win_h
        x = max(0.0, min(1.0, sx / screen_w))
        y = max(0.0, min(1.0, sy / screen_h))
        return x, y
    return _mapped


def inject_from_harvest(
    harvested: dict,
    cap_project_path: str,
    *,
    screen_w: int = DEFAULT_SCREEN_W,
    screen_h: int = DEFAULT_SCREEN_H,
    viewport_w: int = 1280,
    viewport_h: int = 800,
    window_rect: tuple[int, int, int, int] | None = None,
) -> bool:
    """Write cursor.json, copy PNGs, patch recording-meta.json + project-config.json
    from harvested JS tracker data.

    Args:
        harvested: {moves: [{x,y,time}], clicks: [{x,y,time,down,button}], started}
        cap_project_path: path to the .cap project directory
        screen_w/screen_h: screen resolution for coord normalization
        viewport_w/viewport_h: browser viewport in CSS px (tracker's coord system)
        window_rect: (left, top, right, bottom) of the Camoufox window in screen
            px, from GetWindowRect. None falls back to divide-by-screen-only
            normalization (misaligned but non-crashing).
    """
    raw_moves = harvested.get("moves", [])
    raw_clicks = harvested.get("clicks", [])

    if not raw_moves:
        log("[cursor] WARNING: no moves harvested — cursor.json will be empty "
             "(video will have no cursor)")
        return False

    norm = _make_normalizer(
        screen_w=screen_w,
        screen_h=screen_h,
        viewport_w=viewport_w,
        viewport_h=viewport_h,
        window_rect=window_rect,
    )
    if window_rect is not None:
        log(f"[cursor] normalizing coords (screen={screen_w}x{screen_h}, "
            f"viewport={viewport_w}x{viewport_h}, windowRect={window_rect})")
    else:
        log(f"[cursor] normalizing coords (screen={screen_w}x{screen_h}, "
            f"viewport={viewport_w}x{viewport_h}, windowRect=None — fallback divide-by-screen)")

    cursor_id = "0"  # single arrow cursor for the whole recording

    moves_out: list[dict] = []
    for m in raw_moves:
        x, y = norm(float(m["x"]), float(m["y"]))
        moves_out.append({
            # snake_case is what Cap actually deserializes — camelCase alone
            # fails with: missing field `active_modifiers`. See note below.
            "active_modifiers": [],
            "cursor_id": cursor_id,
            "time_ms": float(m["time"]),
            # camelCase duplicates kept for forward/backward compatibility.
            "activeModifiers": [],
            "cursorId": cursor_id,
            "timeMs": float(m["time"]),
            "x": x,
            "y": y,
        })

    # FIELD CASING: the installed Cap build deserializes these structs with
    # snake_case field names, NOT the camelCase that serde rename_all would
    # produce. Emitting camelCase only made `cap export` reject the file:
    #   Failed to parse cursor data: missing field `active_modifiers`
    # ...and silently render no cursor at all. We emit BOTH casings in each
    # event. That is safe because the same error proved Cap does not use
    # deny_unknown_fields: it parsed our camelCase keys without objecting to
    # them as unknown, and complained only about the absent snake_case one.
    # So whichever casing this Cap build wants, it finds it, and ignores the
    # other.
    clicks_out: list[dict] = []
    for c in raw_clicks:
        x, y = norm(float(c["x"]), float(c["y"]))
        # Only record left-button (button=0) clicks for simplicity
        if c.get("button", 0) != 0:
            continue
        clicks_out.append({
            # snake_case is the casing Cap actually deserializes.
            "active_modifiers": [],
            "cursor_num": 0,
            "cursor_id": cursor_id,
            "time_ms": float(c["time"]),
            # camelCase duplicates for compatibility (unknown fields ignored).
            "activeModifiers": [],
            "cursorNum": 0,
            "cursorId": cursor_id,
            "timeMs": float(c["time"]),
            "x": x,
            "y": y,
            "down": bool(c.get("down", True)),
        })

    cursor_json = {"clicks": clicks_out, "moves": moves_out}

    # Write cursor.json into the segment-0 directory
    segment_dir = os.path.join(cap_project_path, "content", "segments", "segment-0")
    cursor_json_path = os.path.join(segment_dir, "cursor.json")
    os.makedirs(segment_dir, exist_ok=True)
    with open(cursor_json_path, "w", encoding="utf-8") as f:
        json.dump(cursor_json, f, indent=2)
    log(f"[cursor] wrote cursor.json: {len(moves_out)} moves, {len(clicks_out)} clicks "
         f"-> {cursor_json_path}")

    # Copy cursor PNGs from apps/web/public/cursors/ into the .cap project.
    # This file lives at scripts/video-agent/src/run/cursor_injection.py, so
    # repo_root (ui-kit) is 4 dirs up, not 2 (the old run.py was 2 dirs up).
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    src_cursors_dir = os.path.join(repo_root, "apps", "web", "public", "cursors")
    dst_cursors_dir = os.path.join(cap_project_path, "content", "cursors")
    os.makedirs(dst_cursors_dir, exist_ok=True)

    copied_pngs: list[str] = []
    for name in ("cursor_0.png", "cursor_1.png", "cursor_2.png"):
        src = os.path.join(src_cursors_dir, name)
        dst = os.path.join(dst_cursors_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, dst)
            copied_pngs.append(name)
    if copied_pngs:
        log(f"[cursor] copied cursor PNGs: {', '.join(copied_pngs)}")
    else:
        log("[cursor] WARNING: no cursor PNGs found to copy")

    # Patch recording-meta.json: add cursors map referencing cursor_0.png
    meta_path = os.path.join(cap_project_path, "recording-meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["cursors"] = {
            "0": {
                "imagePath": "content/cursors/cursor_0.png",
                "hotspot": ARROW_HOTSPOT,
                "shape": ARROW_SHAPE,
            }
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        log(f"[cursor] patched recording-meta.json: cursors map with shape={ARROW_SHAPE}")
    else:
        log(f"[cursor] WARNING: recording-meta.json not found at {meta_path}")

    # Patch project-config.json: ensure cursor settings enable rendering
    config_path = os.path.join(cap_project_path, "project-config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        cursor_cfg = config.get("cursor", {})
        cursor_cfg["hide"] = False
        cursor_cfg["hideWhenIdle"] = False
        cursor_cfg["useSvg"] = True
        cursor_cfg["animationStyle"] = "mellow"
        config["cursor"] = cursor_cfg
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        log(f"[cursor] patched project-config.json: hide=False, animationStyle=mellow, useSvg=True")
    else:
        log(f"[cursor] WARNING: project-config.json not found at {config_path}")

    return True
