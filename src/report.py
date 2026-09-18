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
  steps.jsonl          canonical machine transcript: one validated StepRecord
                       per step (intent, proposed/executed calls, result,
                       observable change, error, recovery, replayable flag)
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

import itertools as _itertools

_seq = _itertools.count(1)


def _envelope(run_dir: str, obj: dict[str, Any]) -> dict[str, Any]:
    """Join-key envelope for every JSONL line: ``run_id`` (the run-dir
    name) + a process-global ``seq``.

    ``setdefault`` semantics so callers and tests can pin values. This
    is what turns the five append-only streams (steps, transcript,
    wire, payload, frontier) into one queryable trajectory:
    ``jq -s 'sort_by(.seq)'``. Snapshots (run.json, frontier.json)
    stay envelope-free — they are derived caches, not stream members.
    """
    out = dict(obj)
    out.setdefault("run_id", os.path.basename(os.path.abspath(run_dir)))
    out.setdefault("seq", next(_seq))
    return out


class StepPayload(BaseModel):
    """Typed per-step record: everything Jev saw, asked, and answered.

    Written as one JSON object per line (step-NN-payload.jsonl) so rankings
    (Choice probability distributions) stay machine-parseable — no more
    .txt dumps. Unknown extra keys are ignored on read for forward compat.
    """

    model_config = {"extra": "ignore"}

    n: int = Field(ge=1)
    t: float = 0.0
    run_id: str = ""
    seq: int = 0
    url: str = ""
    task: str = ""
    state: dict[str, Any] = Field(default_factory=dict)
    questions: dict[str, Any] = Field(default_factory=dict)
    answers: dict[str, Any] = Field(default_factory=dict)
    decision: str = ""
    phases: list[str] = Field(default_factory=list)


class ProposedCall(BaseModel):
    """What the proposer/writer suggested before Jev voted."""

    model_config = {"extra": "ignore"}

    kind: str = ""
    item: int | None = None
    url: str | None = None
    rationale: str = ""


class ExecutedCall(BaseModel):
    """The tool call that actually ran (post-gate, post-override)."""

    model_config = {"extra": "ignore"}

    kind: str = ""
    item: int | None = None
    ref: str | None = None
    selector: str | None = None
    url: str | None = None


class RecoveryAttempt(BaseModel):
    """One audited compensation (heal, challenge-work, recover, synthesis)."""

    model_config = {"extra": "ignore"}

    strategy: str = ""
    result: str = ""
    reason: str = ""


class StepRecord(BaseModel):
    """Canonical machine-readable transcript: exactly one JSON object per
    step per line (steps.jsonl). No human log prose, no raw DOM dumps —
    compact excerpts and references only, sufficient to reconstruct the
    run offline: what was tried, what ran, what changed, what failed,
    what compensated, and whether the step can be re-driven.

    Replayable means a fresh session can re-drive from this step: the
    resulting URL is known and the action is deterministic (goto URL, or
    click/type/challenge grounded by a durable selector — never a bare
    positional idx, which dies with its probe).
    """

    model_config = {"extra": "ignore"}

    n: int = Field(ge=1)
    t: float = 0.0
    run_id: str = ""
    seq: int = 0
    url: str = ""
    url_after: str = ""
    intent: str = ""
    page_state: dict[str, Any] = Field(default_factory=dict)
    proposed: ProposedCall = Field(default_factory=ProposedCall)
    decided: dict[str, Any] = Field(default_factory=dict)
    executed: ExecutedCall = Field(default_factory=ExecutedCall)
    action_type: str = ""
    result: str = ""
    observable_change: bool = False
    error: str | None = None
    recovery: RecoveryAttempt | None = None
    captcha_ocr: dict[str, Any] = Field(default_factory=dict)
    shield: dict[str, Any] = Field(default_factory=dict)
    twocaptcha: dict[str, Any] = Field(default_factory=dict)
    grid: dict[str, Any] = Field(default_factory=dict)
    solver_rank: dict[str, Any] = Field(default_factory=dict)
    frontier: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    replayable: bool = False
    verdict: str = ""
    phases: list[str] = Field(default_factory=list)


REPLAYABLE_KINDS = frozenset({"goto", "click_item", "type_at", "challenge"})


def write_step_jsonl(run_dir: str, record: dict[str, Any]) -> str:
    """Validate one canonical step record and append it to steps.jsonl.

    Pydantic-validated on write like StepPayload: schema breaks fail
    loudly in tests; at runtime the raw dict still lands for forensics.
    """
    path = os.path.join(run_dir, "steps.jsonl")
    record = _envelope(run_dir, record)
    try:
        line = StepRecord.model_validate(record).model_dump_json()
    except ValidationError:
        line = json.dumps(record, ensure_ascii=False)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return path


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
    obj = _envelope(run_dir, {"n": n, "t": t, "url": url, "task": task,
                              "state": state or {}, "questions": questions or {},
                              "answers": answers or {}, "decision": decision,
                              "phases": phases or []})
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


def write_captcha_json(run_dir: str, n: int, payload: dict[str, Any]) -> str:
    """Write the structured CAPTCHA analysis for one step (captcha-NN.json).

    Payload carries the step number, URL, element idx, and the OCR record
    (kind, text, boxes, confidence, backend, suggestion). No image bytes —
    only the analysis, so the file stays small and greppable. Fail-soft
    is the caller's job; this writer assumes a valid payload dict.
    """
    path = os.path.join(run_dir, f"captcha-{n:02d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


def write_shield_json(run_dir: str, n: int, payload: dict[str, Any]) -> str:
    """Write the structured shield-solve analysis for one step (shield-NN.json).

    Payload carries the step number, URL, element idx, and the solve
    record (challenge_type, success, token_len, backend notes). Token
    values are never recorded — lengths only. Same audit contract as
    write_captcha_json.
    """
    path = os.path.join(run_dir, f"shield-{n:02d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


def write_frontier_event(run_dir: str, event: dict[str, Any]) -> str:
    """Append one crawl-frontier ledger event (frontier.jsonl).

    The canonical to-do record: ``discovered`` (new link target queued),
    ``visited`` (navigation landed), ``aliased`` (label→URL learned).
    One JSON object per line, crash-safe append, replayable offline.
    """
    path = os.path.join(run_dir, "frontier.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(_envelope(run_dir, event), ensure_ascii=False) + "\n")
    return path


def write_memory_jsonl(run_dir: str, rows: list[dict[str, Any]]) -> str:
    """Append scored page-memory chunks (memory.jsonl, enveloped).

    The machine companion to MEMORY.md: one line per chunk with
    run_id/seq/url/section/text/score/source, queryable like every
    other trajectory stream. Snapshots never land here — appends only.
    """
    path = os.path.join(run_dir, "memory.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        for row in rows or []:
            f.write(json.dumps(_envelope(run_dir, dict(row)),
                               ensure_ascii=False) + "\n")
    return path


def write_frontier_snapshot(run_dir: str, snapshot: dict[str, Any]) -> str:
    """Rewrite the full frontier state (frontier.json).

    Snapshot for cheap resume and human inspection; the JSONL event
    ledger above is the audit trail. Atomic-ish: write tmp + rename so
    a mid-write crash never leaves a corrupt ledger.
    """
    path = os.path.join(run_dir, "frontier.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def _public_entry(entry: dict) -> dict:
    """Drop private ``_``-prefixed keys before serializing.

    The runner split passes live objects (machine, phase trail) through
    the step entry dict; those must never reach JSON artifacts — the
    original flat runner kept them as closure locals, so stripping
    restores exactly the old on-disk shape.
    """
    return {k: v for k, v in entry.items() if not str(k).startswith("_")}


def append_transcript(run_dir: str, entry: dict) -> None:
    with open(os.path.join(run_dir, "transcript.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(_envelope(run_dir, _public_entry(entry)),
                           ensure_ascii=False) + "\n")


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
        f.write(json.dumps(_envelope(run_dir, _public_entry(wire)),
                           ensure_ascii=False) + "\n")
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


# Trajectory reads live in src.trajectory (replay + lessons); report.py
# keeps byte-writers only. Re-exported here so existing imports
# (run.py, loop.py, scripts, tests) keep working unchanged.
from .trajectory import LESSONS_LIMIT, load_lessons, replay_payload


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
