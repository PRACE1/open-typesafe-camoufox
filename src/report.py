"""
report.py — run folder + offline-replay artifacts (typesafe's report.py equivalent).

Every run writes runs/<timestamp>/:
  run.log / run.json   everything printed; goal, outcome, seconds, config
  step-NN-raw.png      the screenshot capture
  step-NN.png          elements numbered in blue, the chosen one red,
                       the focused field outlined green
  step-NN-payload.txt  the exact state + criteria sent to Jev, then every
                       element with center + the decoded decision
  step-NN-answers.json every probability the classifier returned
  transcript.jsonl     one JSON line per step (kept from the legacy loop)
  cursor.json          video-agent-compatible cursor trail

A stall replays offline from step-NN-payload.txt + step-NN-answers.json
without touching the screen (see --replay in run.py).
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime
from typing import Any

from .deps import ElementRef


def make_run_dir() -> str:
    root = os.path.abspath(os.path.join(os.getcwd(), "runs"))
    os.makedirs(root, exist_ok=True)
    d = os.path.join(root, datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    return d


def write_raw_png(run_dir: str, n: int, png_bytes: bytes) -> str:
    path = os.path.join(run_dir, f"step-{n:02d}-raw.png")
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path


def annotate_png(png_bytes: bytes, elements: list[ElementRef],
                 chosen_idx: int | None = None,
                 focused_frame: dict | None = None) -> bytes:
    """Blue idx rings, chosen element red, focused field green."""
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    for e in elements:
        x, y = e.cx * w, e.cy * h
        r = 14
        color = "red" if (chosen_idx is not None and e.idx == chosen_idx) else "blue"
        d.ellipse([x - r, y - r, x + r, y + r], outline=color, width=3)
        d.text((x + r + 2, y - r), str(e.idx), fill=color)
    if focused_frame:
        try:
            x, y = int(focused_frame.get("x", 0)), int(focused_frame.get("y", 0))
            ww, hh = int(focused_frame.get("w", 0)), int(focused_frame.get("h", 0))
            if ww > 2 and hh > 2:
                d.rectangle([x, y, x + ww, y + hh], outline="green", width=3)
        except (ValueError, TypeError, AttributeError):
            pass
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def write_annotated_png(run_dir: str, n: int, png_bytes: bytes,
                        elements: list[ElementRef], chosen_idx: int | None = None,
                        focused_frame: dict | None = None) -> str:
    path = os.path.join(run_dir, f"step-{n:02d}.png")
    try:
        out = annotate_png(png_bytes, elements, chosen_idx, focused_frame)
    except Exception:
        out = png_bytes
    with open(path, "wb") as f:
        f.write(out)
    return path


def write_payload_txt(run_dir: str, n: int, state: dict[str, Any],
                      questions: dict[str, Any], decision: str) -> str:
    path = os.path.join(run_dir, f"step-{n:02d}-payload.txt")
    lines = [
        f"=== step {n} state ===",
        json.dumps(state, ensure_ascii=False, indent=1),
        "",
        "=== questions (criteria) ===",
        json.dumps(questions, ensure_ascii=False, indent=1),
        "",
        "=== decision ===",
        decision,
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def write_answers_json(run_dir: str, n: int, raw: dict[str, Any]) -> str:
    path = os.path.join(run_dir, f"step-{n:02d}-answers.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    return path


def append_transcript(run_dir: str, entry: dict) -> None:
    with open(os.path.join(run_dir, "transcript.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def write_wire(run_dir: str, wire: dict[str, Any]) -> str:
    """Append one normalized wire record per step (wire.jsonl).

    The machine-readable twin of the human feed: normalized URL, tabs,
    full element map (idx/kind/label/text/value/href/region/host/sel/coords),
    focused field, page text, decision + Nouls + progress, action, result,
    phases, notes, visited. Grep-able and replayable offline without parsing
    the annotated payload dumps.
    """
    path = os.path.join(run_dir, "wire.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(wire, ensure_ascii=False) + "\n")
    return path


def write_cursor(run_dir: str, cursor: dict) -> str:
    path = os.path.join(run_dir, "cursor.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cursor, f)
    return path


def write_run_json(run_dir: str, summary: dict) -> str:
    path = os.path.join(run_dir, "run.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    return path


def replay_payload(run_dir: str, n: int) -> dict[str, Any]:
    """Load a saved step's payload + answers for offline debugging."""
    out: dict[str, Any] = {}
    answers_path = os.path.join(run_dir, f"step-{n:02d}-answers.json")
    if os.path.exists(answers_path):
        with open(answers_path, encoding="utf-8") as f:
            out["answers"] = json.load(f)
    return out


def append_memory(run_dir: str, line: str) -> str:
    """Append one finding to the run's notebook (runs/<ts>/MEMORY.md).

    The per-run notebook: extractive findings banked as the loop reads new
    pages, plus the final outcome. Survives the process for replay/debugging,
    unlike the in-memory notes buffer.
    """
    path = os.path.join(run_dir, "MEMORY.md")
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")
    return path


LESSONS_LIMIT = 1200  # chars of the shared lessons file injected per call


def load_lessons(root: str, limit: int = LESSONS_LIMIT) -> str:
    """Bounded excerpt of the shared cross-run lessons notebook.

    `.agent-memory/MEMORY.md` holds durable hard-won facts (this build's
    quirks, gate thresholds, loop shapes). Bounded like the doc's notebook
    model: latest snapshot per run, never dumped whole, so the packet can't
    bloat across 40-50 steps.
    """
    path = os.path.join(root, ".agent-memory", "MEMORY.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut if cut else text[:limit]
