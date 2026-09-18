"""runner.gates — confidence gating, reading cooldown, and recovery selection.

These are the pure decision gates that sit between DECIDE and ACT in the
step loop. Each returns a boolean or a string strategy; none mutate state.
"""

from __future__ import annotations

from ..decide import Kind
from .constants import GATE_EXEMPT_KINDS, READING_COOLDOWN_STEPS


def confidence_gated(kind: Kind, conf: float, min_confidence: float) -> bool:
    """True when a low-confidence decision must idle instead of acting."""
    return conf < min_confidence and kind not in GATE_EXEMPT_KINDS


def reading_cooldown_active(steps: int, until_step: int) -> bool:
    """True while the loop must stay on its adopted page and read."""
    return steps <= until_step


def select_recovery(reason: str) -> str | None:
    """Compensating action for a noop reason, or None to let rails decide.

    blank/failed-same/fixation refresh the page (stale content is the
    common cause); covered escapes once (structural overlays survive
    per-action dismisses). Blocked pages belong to the challenge flow,
    mismatches to escalation — recovery stays out of both. First match.
    """
    if reason in ("blank", "failed-same", "fixation"):
        return "refresh"
    if reason == "covered":
        return "escape"
    return None
