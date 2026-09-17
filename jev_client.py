"""
Jev decider client (TypeSafe System One model).

Exact API per https://docs.typesafe.ai/api:
  POST https://api.typesafe.ai/v1/systemone
  {"state": str|object|array, "model": "jev-latest",
   "questions": {"<id>": {"type": "choice"|"score"|"noul", ...}}}
  -> {"answers": {"<id>": {"type":..., "choice"|"score"|"noul", ...}}, "usage": {...}}

This harness sends ONE Jev call per sampled keyframe. The frame travels as
BRAILLE TEXT (jev_ascii.frame_to_braille, math ported from prime-agent
scripts/render-logo.py --style braille) — the deterministic version of the
trick non-vision models improvise via prime-agent's attach-image kernel
fallback ("open it in the kernel with PIL"). Text Jev can always read;
no vision capability required on the decider side.

  state     = {"prompt": ..., "frame_text": "<braille>", "grid": "8x8 over {cols}x{rows}"}
  questions = {cursor_cell: Choice over 8x8 grid (64 options, under Jev's 255 cap),
               click: Noul, actionability: Score}
  x,y decoded from winning cell; click/move gated on calibrated confidence.

429/529 retried with exponential backoff (SDK-equivalent default).
No-key fallback returns a deterministic heuristic so the loop stays testable offline.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

import httpx

from jev_ascii import grid_span

TYPESAFE_BASE_URL = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1")
TYPESAFE_API_KEY = os.environ.get("TYPESAFE_API_KEY", "")
TYPESAFE_MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")

GRID = 8  # 8x8 = 64 Choice options (Jev cardinality cap is 255)


@dataclass
class JevDecision:
    x: float  # 0-1
    y: float  # 0-1
    click: bool
    confidence: float  # 0-1 calibrated
    raw: dict


def _decode_cell(cell: int) -> tuple[float, float]:
    cell = max(0, min(GRID * GRID - 1, int(cell)))
    gx, gy = cell % GRID, cell // GRID
    return (gx + 0.5) / GRID, (gy + 0.5) / GRID


def _cell_criteria(cols: int, rows: int) -> dict[str, str]:
    return {str(i): grid_span(i, GRID, cols, rows) for i in range(GRID * GRID)}


async def _post(payload: dict, timeout_s: float) -> dict:
    headers = {"Authorization": f"Bearer {TYPESAFE_API_KEY}"}
    backoff = 1.0
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for attempt in range(4):
            res = await client.post(
                f"{TYPESAFE_BASE_URL.rstrip('/')}/systemone", json=payload, headers=headers
            )
            if res.status_code in (429, 529) and attempt < 3:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            res.raise_for_status()
            return res.json()
    raise RuntimeError("unreachable")


async def decide_cursor(
    frame_text: str,
    *,
    prompt: str = "Move the cursor to the most actionable element.",
    cols: int = 0,
    rows: int = 0,
    threshold: float = 0.7,
    timeout_s: float = 30.0,
) -> JevDecision:
    if not TYPESAFE_API_KEY:
        return JevDecision(x=0.5, y=0.45, click=False, confidence=0.0, raw={"fallback": "no-key"})

    payload: dict[str, Any] = {
        "model": TYPESAFE_MODEL,
        "state": {
            "prompt": prompt,
            "frame_text": frame_text,
            "grid": f"{GRID}x{GRID} over {cols}x{rows} chars; each option names its char span",
        },
        "questions": {
            "cursor_cell": {
                "type": "choice",
                "instructions": "Which grid cell holds the most actionable element for the cursor? Ground your answer in the braille frame text spans.",
                "criteria": _cell_criteria(cols, rows),
            },
            "click": {
                "type": "noul",
                "instructions": "Should the cursor click once it arrives?",
                "criteria": {"true": "Clicking advances the task", "false": "Just hover, do not click"},
            },
            "actionability": {
                "type": "score",
                "instructions": "How actionable is the framed element?",
                "criteria": ["Not actionable", "Somewhat actionable", "Highly actionable"],
            },
        },
    }
    data = await _post(payload, timeout_s)
    answers = data.get("answers", {})

    cell_raw = answers.get("cursor_cell", {})
    try:
        cell = int(cell_raw.get("choice", "32"))
    except (ValueError, TypeError):
        cell = 32
    conf = float(cell_raw.get("confidence", 0.5) or 0.5)

    click_raw = answers.get("click", {})
    click_noul = float(click_raw.get("noul", 0.0) or 0.0)

    score_raw = answers.get("actionability", {})
    try:
        score = float(score_raw.get("score", 0.0) or 0.0)
    except (ValueError, TypeError):
        score = 0.0

    x, y = _decode_cell(cell)
    click = click_noul >= threshold and conf >= threshold
    return JevDecision(x=x, y=y, click=click, confidence=min(max(conf, 0.0), 1.0), raw=data)
