"""
report.py — run folder + offline-replay artifacts (typesafe's report.py equivalent).

Every run writes runs/<timestamp>/:
  run.log / run.json   everything printed; goal, outcome, seconds, config
  step-NN-raw.png      the screenshot capture
  step-NN.png          elements numbered in blue, the chosen one red,
                       the focused field outlined green
  step-NN-payload.jsonl  one pydantic-validated JSON line per step: state +
                       questions + full Jev answers (rankings) + decision
  step-NN-answers.json every probability the classifier returned
  transcript.jsonl     one JSON line per step (kept from the legacy loop)
  cursor.json          video-agent-compatible cursor trail

A stall replays offline from step-NN-payload.jsonl + step-NN-answers.json
without touching the screen (see --replay in run.py).
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from .deps import ElementRef


class StepPayload(BaseModel):
    """Typed per-step record: everything Jev saw, asked, and answered.

    Written as one JSON object per line (step-NN-payload.jsonl) so rankings
    (Choice probability distributions) stay machine-parseable — no more
    .txt dumps. Unknown extra keys are ignored on read for forward compat.
    """

    model_config = {"extra": "ignore"}

    n: int = Field(ge=1)
    t: float = 0.0
    url: str = ""
    task: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    questions: dict[str, Any] = Field(default_factory=dict)
    answers: dict[str, Any] = Field(default_factory=dict)
    decision: str = ""
    phases: list[str] = Field(default_factory=list)


def write_payload_jsonl(run_dir: str, n: int, *, t: float = 0.0,
                        url: str = "", task: str = "",
                        state: dict | None = None,
                        questions: dict | None = None,
                        answers: dict | None = None,
                        decision: str = "",
                        phases: list[str] | None = None) -> str:
    """Validate one step record and append it as JSONL (one line per step).

    Pydantic-validated on write: a schema break fails loudly in tests and
    is caught here so a run never dies on its own telemetry — the raw dict
    still lands for forensics.
    """
    path = os.path.join(run_dir, f"step-{n:02d}-payload.jsonl")
    obj = {"n": n, "t": t, "url": url, "task": task,
           "state": state or {}, "questions": questions or {},
           "answers": answers or {}, "decision": decision,
           "phases": phases or []}
    try:
        line = StepPayload.model_validate(obj).model_dump_json()
    except ValidationError:
        line = json.dumps(obj, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


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
    """Acted ref box (blue), chosen element red, focused field green.

    Boxes are banked at dispatch time (lazy aria resolution); elements
    never acted on carry box=None and are skipped. Replays of older runs
    fall back to cx/cy rings.
    """
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    d = ImageDraw.Draw(img)
    for e in elements:
        color = "red" if (chosen_idx is not None and e.idx == chosen_idx) else "blue"
        if e.box is not None:
            bx, by, bw, bh = e.box
            x0, y0 = bx * w, by * h
            d.rectangle([x0, y0, x0 + bw * w, y0 + bh * h],
                        outline=color, width=3)
            d.text((x0 + 2, y0 - 10), e.ref or str(e.idx), fill=color)
        else:
            x, y = e.cx * w, e.cy * h
            r = 14
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
    full element map (idx/ref/kind/label/text/value/href/region/host/coords),
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
    payload_path = os.path.join(run_dir, f"step-{n:02d}-payload.jsonl")
    if os.path.exists(payload_path):
        with open(payload_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out["payload"] = StepPayload.model_validate_json(line).model_dump()
                except (ValidationError, ValueError):
                    try:
                        out["payload"] = json.loads(line)
                    except ValueError:
                        continue
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
