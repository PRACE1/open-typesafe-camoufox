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

from collections import deque

from statemachine import State, StateMachine


class RunMachine(StateMachine):
    """One step walks see -> decide -> gate -> [act] -> verify.

    A noop step with budget left detours verify -> recover -> see: exactly
    one compensating dispatch (refresh/escape/back) after fresh observation,
    never a blind repeat. See docs/PYDANTIC_AI_CONTRACT.md section 8.
    """

    see = State(initial=True)
    decide = State()
    gate = State()
    act = State()
    verify = State()
    heal = State()
    recover = State()
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
    healed = heal.to(act, cond="healed_target_ready")
    heal_failed = heal.to(verify)
    recover_needed = verify.to(recover)
    recovered = recover.to(see)
    continue_run = verify.to(see)

    def healed_target_ready(self) -> bool:
        """Guard: re-enter act only with a remapped target in hand.

        The runner sets `machine.healed_target_ready = True` after a
        successful label remap, False otherwise. A guarded `healed` with no
        target raises TransitionNotAllowed instead of retrying blind.
        """
        return bool(getattr(self, "healed_target_ready_flag", False))

    def on_enter_heal(self, **kwargs) -> None:
        """Telemetry hook: count heal entries per session for the run log."""
        self.heal_attempts = getattr(self, "heal_attempts", 0) + 1

    def on_enter_recover(self, **kwargs) -> None:
        """Telemetry hook: count recovery entries per session."""
        self.recover_attempts = getattr(self, "recover_attempts", 0) + 1

    def after_transition(self, **kwargs) -> None:
        """Forensics hook: bounded log of (source, target) id pairs.

        The runner dumps this on honest stops; tests assert the heal cycle
        appears here end to end.
        """
        log = getattr(self, "transitions_log", None)
        if log is None:
            log = deque(maxlen=64)
            self.transitions_log = log
        try:
            ed = kwargs.get("event_data")
            log.append((str(ed.transition.source.id),
                        str(ed.transition.target.id)))
        except (AttributeError, TypeError):
            log.append(("?", "?"))


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
    "verify": {"see", "done", "stopped", "recover"},
    "recover": {"see"},
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
