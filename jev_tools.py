"""
Jev-validated tool schemas for open-typesafe-camoufox.

Exactly TWO tools — nothing else is whitelisted in jev_agent.py:
  1. ReadFrameAction  (jev read — still frame in, typed decision out)
  2. MoveCursorAction (x/y long-lat normalized 0-1 + optional click, humanize=true)

Ported from ui-kit/scripts/video-agent/tool_schemas.py (5 tools -> 2 tools).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReadFrameAction(BaseModel):
    """Capture one still frame for the Jev decider.

    Capture runs at 60fps in the browser, but Jev inference is sampled at
    2-5fps (Jev p70-500ms per call). The runner decimates to keyframes.
    """

    fps: float = Field(
        3.0,
        ge=2.0,
        le=5.0,
        description="Sample rate for Jev inference. Capture stays at 60fps; only keyframes are sent.",
    )
    format: Literal["jpeg"] = Field(
        "jpeg",
        description="Still encoding. JPEG only (keeps base64 payload small).",
    )
    max_width: int = Field(
        1280, ge=320, le=1920, description="Downscale width before sending to Jev."
    )
    width_cols: int = Field(
        80, ge=20, le=160, description="Braille text width in characters (render-logo.py --width)."
    )
    threshold: int = Field(
        128, ge=0, le=255, description="Luminance cutoff 0-255; raise for thinner strokes (render-logo.py --threshold)."
    )


class MoveCursorAction(BaseModel):
    """Move the x/y cursor, optionally click/type/press-key. humanize always on.

    x = longitude (horizontal, 0 left -> 1 right).
    y = latitude (vertical, 0 top -> 1 bottom).
    Normalized to viewport/screen 0.0-1.0, matching video-agent cursor.json.

    element: optional idx into the element map (find_elements). When set, the
    capability drives the video-agent pattern for that element:
    scroll-into-view -> circle-highlight (8 sweeps) -> click -> type -> key.
    x/y are then ignored (re-derived from the element's live bounding box).

    Typing rides along with the move so the tool count stays at exactly two:
    move (optionally click the field first), then type, then optional key.
    """

    x: float = Field(..., ge=0.0, le=1.0, description="Horizontal position 0-1. Ignored when element is set.")
    y: float = Field(..., ge=0.0, le=1.0, description="Vertical position 0-1. Ignored when element is set.")
    click: bool = Field(
        False, description="If true, left-click after the humanized move."
    )
    humanize: bool = Field(
        True, description="Camoufox humanize trajectory. Kept on for recordings."
    )
    element: int | None = Field(
        None, ge=0, description="Element idx from the element map: scroll+circle+click that element."
    )
    type_text: str | None = Field(
        None,
        max_length=500,
        description="If set, click first (focus), then type this text. Secrets are masked in logs.",
    )
    key: Literal["Enter", "Tab", "Escape"] | None = Field(
        None, description="If set, press this key after any typing (e.g. Enter to submit)."
    )


class JevSolverTool(BaseModel):
    """One whitelisted tool call. Exactly one action is set."""

    read_frame: ReadFrameAction | None = None
    move_cursor: MoveCursorAction | None = None
