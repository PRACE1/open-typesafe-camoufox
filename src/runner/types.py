"""runner.types — Phase enum and phase-step helper.

The phase vocabulary is the transcript's axis of record. Transition
truth lives in ``src/machine/run_engine.py`` (RunMachine + EDGES);
``phase_step`` below is a thin adapter over that table so the classic
walks keep working, and the loop body emits through ``advance()``.
"""

from __future__ import annotations

from enum import Enum

from ..machine.run_engine import EDGES as _ENGINE_EDGES


class Phase(str, Enum):
    """Run-loop phase names — the transcript vocabulary.

    Every step walks SEE → DECIDE → GATE → [ACT] → VERIFY and ends in
    DONE/STOPPED. Idle paths skip ACT (GATE → VERIFY). DONE is accepted
    in GATE; stops are decided in VERIFY (or STEER). A stale/covered
    click detours ACT → HEAL → ACT (one re-attempt) or ACT → HEAL → VERIFY.
    """

    SEE = "see"
    DECIDE = "decide"
    GATE = "gate"
    ACT = "act"
    VERIFY = "verify"
    HEAL = "heal"
    RECOVER = "recover"
    DONE = "done"
    STOPPED = "stopped"


def phase_step(phases: list[str], nxt: Phase | str) -> list[str]:
    """Append a phase transition; raise on an illegal move.

    Validates against the engine's edge table (single source of truth),
    so a broken loop fails loudly instead of drifting.
    """
    nxt_v = nxt.value if isinstance(nxt, Phase) else str(nxt)
    if not phases:
        if nxt_v != Phase.SEE.value:
            raise ValueError(f"run must start at SEE, got {nxt_v}")
    elif nxt_v not in _ENGINE_EDGES.get(phases[-1], set()):
        raise ValueError(f"illegal phase transition {phases[-1]} -> {nxt_v}")
    phases.append(nxt_v)
    return phases
