"""run_engine.py — declarative run-loop state machine (python-statemachine).

Authoritative transition table for the otc run loop; mirrors
src/machine/run_machine.ts. The runner drives it event-by-event — one
machine per session — and records state ids into the transcript `phases`
trail (same strings Phase produced before it, so replay/report are
unchanged). Illegal moves raise TransitionNotAllowed: fail loud, never
drift.

States carry no data; all counters/memory stay in the runner (context-out
pattern from the TS spec). The `heal` state is the self-healing retry:
a stale/covered action transitions acting -> healing, then either back to
acting for one re-attempt or on to verifying to account the failure.
"""

from __future__ import annotations

from statemachine import State, StateMachine


class RunMachine(StateMachine):
    """One step walks see -> decide -> gate -> [act] -> verify."""

    see = State(initial=True)
    decide = State()
    gate = State()
    act = State()
    verify = State()
    heal = State()
    done = State(final=True)
    stopped = State(final=True)

    perceived = see.to(decide)
    decided = decide.to(gate)
    act_now = gate.to(act)
    idle_now = gate.to(verify)
    finish = gate.to(done) | verify.to(done)
    abort = gate.to(stopped) | verify.to(stopped) | see.to(stopped)
    acted = act.to(verify)
    heal_needed = act.to(heal)
    healed = heal.to(act)
    heal_failed = heal.to(verify)
    continue_run = verify.to(see)


def state_id(machine: RunMachine) -> str:
    """Current state id for transcripts (avoids the deprecated current_state)."""
    return str(next(iter(machine.configuration)).id)


#: Transition truth for the whole run loop, keyed by source state id.
#: The runner's phase_step adapter validates against this table; the
#: test_machine.py edge-table test proves the machine matches it exactly.
EDGES: dict[str, set[str]] = {
    "see": {"decide", "stopped"},
    "decide": {"gate"},
    "gate": {"act", "verify", "done", "stopped"},
    "act": {"verify", "heal"},
    "heal": {"act", "verify"},
    "verify": {"see", "done", "stopped"},
    "done": set(),
    "stopped": set(),
}


def legal(prev: str, nxt: str) -> bool:
    """True when prev -> nxt is a declared edge (start: only see)."""
    if not prev:
        return nxt == "see"
    return nxt in EDGES.get(prev, set())


def advance(machine: RunMachine, trail: list[str], event: str) -> list[str]:
    """Fire a transition event; record the resulting state.

    Raises TransitionNotAllowed on illegal moves (fail loud), AttributeError
    on unknown event names (programmer error, also loud).
    """
    getattr(machine, event)()
    trail.append(state_id(machine))
    return trail
