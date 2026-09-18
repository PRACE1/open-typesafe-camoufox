"""replay.py — offline reads of a saved run (moved out of report.py).

No writers here: replay consumes the trajectory streams
(step-NN-payload.jsonl + step-NN-answers.json) without touching live
state. Pure move from report.replay_payload; behavior unchanged.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import ValidationError


def replay_payload(run_dir: str, n: int) -> dict[str, Any]:
    """Load a saved step's payload + answers for offline debugging."""
    # Lazy: report.py re-exports this module, so a top-level import
    # would cycle (report → trajectory → report).
    from ..report import StepPayload

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
