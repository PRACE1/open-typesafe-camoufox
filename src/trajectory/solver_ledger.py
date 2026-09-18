"""solver_ledger.py — solver effectiveness derived from /runs (no sidecar).

Single source of truth: ``runs/*/steps.jsonl`` already stores every
solver outcome (shield/grid/twocaptcha blocks carry ``success``;
captcha_ocr carries actionable output; toggle outcomes read off the
result text). There is deliberately NO separate ledger file — a
sidecar would diverge from the canonical record it claims to
summarize.

``stats()`` folds all runs into per-solver ``{attempts, successes,
rate}``; ``rank_names()`` reorders backends so repeated failures
demote a solver instead of retrying it forever. Roots resolve from
``OTC_RUNS_DIR`` (tests pin this at an empty tmp dir) or ``cwd/runs``.
Missing/corrupt runs read as {} — offline-safe, never fatal.
"""

from __future__ import annotations

import glob
import json
import os
from typing import Any

MIN_ATTEMPTS_RANKED = 2

SOLVER_SHIELD = "capability:shield-bypass:checkbox"
SOLVER_GRID = "capability:captchakraken:grid"
SOLVER_TWOCAPTCHA = "capability:2captcha-python:solve"
SOLVER_OCR = "capability:ddddocr:ocr"
SOLVER_TOGGLE = "actions:toggle"

# Operator priors: lived experience until runs evidence overrides.
# Vision-via-CaptchaKraken clears grids (~0.8); native shield polls and
# ddddocr text-OCR have never cleared an image grid (0.0). Unlisted
# solvers fall back to the neutral 0.5. Evidence (MIN_ATTEMPTS_RANKED+
# attempts) always wins over priors.
PRIORS = {
    SOLVER_GRID: 0.8,
    SOLVER_SHIELD: 0.0,
    SOLVER_OCR: 0.0,
}


def runs_root(root: str = "") -> str:
    """Runs directory: explicit root, ``OTC_RUNS_DIR``, or ``cwd/runs``."""
    if root:
        return root
    return os.environ.get("OTC_RUNS_DIR", "") or os.path.join(
        os.getcwd(), "runs")


def _block_success(block: Any) -> bool | None:
    """success bool from a shield/grid/twocaptcha block, None if absent."""
    if not isinstance(block, dict) or not block:
        return None
    if isinstance(block.get("success"), bool):
        return block["success"]
    return None


def _ocr_success(block: Any) -> bool | None:
    """OCR counts when it produced actionable output for JEV review."""
    if not isinstance(block, dict) or not block:
        return None
    if not block.get("available") or block.get("error"):
        return False
    data = block.get("data") or {}
    actionable = bool(
        str(block.get("text") or "").strip()
        or block.get("boxes") or block.get("candidates")
        or block.get("suggest_kind")
        or (isinstance(data, dict) and data.get("tiles")))
    return actionable


def _toggle_success(result: str) -> bool | None:
    """Toggle ran iff its verdict line is present; error prefix = fail."""
    text = str(result or "")
    if "challenge checkbox toggled" in text and not text.startswith("error"):
        return True
    if text.startswith("error toggling challenge checkbox"):
        return False
    return None


def outcomes_from_record(record: dict[str, Any]) -> list[tuple[str, bool]]:
    """(solver, success) pairs evidenced by one steps.jsonl record."""
    out: list[tuple[str, bool]] = []
    for key, name in (("shield", SOLVER_SHIELD), ("grid", SOLVER_GRID),
                      ("twocaptcha", SOLVER_TWOCAPTCHA)):
        verdict = _block_success(record.get(key))
        if verdict is not None:
            out.append((name, verdict))
    verdict = _ocr_success(record.get("captcha_ocr"))
    if verdict is not None:
        out.append((SOLVER_OCR, verdict))
    verdict = _toggle_success(record.get("result", ""))
    if verdict is not None:
        out.append((SOLVER_TOGGLE, verdict))
    return out


def stats(root: str = "") -> dict[str, dict]:
    """Per-solver {attempts, successes, rate} across all runs' steps."""
    out: dict[str, dict] = {}
    try:
        files = sorted(glob.glob(os.path.join(runs_root(root),
                                               "*", "steps.jsonl")))
    except OSError:
        return {}
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            try:
                pairs = outcomes_from_record(record)
            except Exception:  # noqa: BLE001
                continue
            for name, success in pairs:
                acc = out.setdefault(
                    name, {"attempts": 0, "successes": 0})
                acc["attempts"] += 1
                if success:
                    acc["successes"] += 1
    for acc in out.values():
        acc["rate"] = (acc["successes"] / acc["attempts"]
                       if acc["attempts"] else 0.0)
    return out


def score_for(name: str, table: dict[str, dict]) -> float:
    """Ranking score: empirical rate once proven, operator prior before.

    Solvers with < MIN_ATTEMPTS_RANKED attempts score their PRIORS
    entry (or neutral 0.5 when unlisted), so cold-start order already
    reflects lived experience — grid-via-CaptchaKraken first, shield
    and text-OCR sunk for grids — instead of pretending all backends
    are equal until they fail live.
    """
    acc = table.get(name)
    if acc is None or acc["attempts"] < MIN_ATTEMPTS_RANKED:
        return float(PRIORS.get(name, 0.5))
    return float(acc["rate"])


def rank_names(names: list[str], table: dict[str, dict]) -> list[str]:
    """Stable reorder of backend names by ledger score (desc).

    Python's sort is stable: ties (including all-neutral priors) keep
    the caller's default order, so behavior is identical until real
    evidence accumulates.
    """
    return sorted(names, key=lambda n: score_for(n, table), reverse=True)
