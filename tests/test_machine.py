"""Tests for the declarative run engine (no browser, no network)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.machine.run_engine import RunMachine, advance, state_id
from statemachine.exceptions import TransitionNotAllowed


def walk(machine, *events):
    trail: list[str] = [state_id(machine)]
    for ev in events:
        advance(machine, trail, ev)
    return trail


def test_happy_path_walk():
    m = RunMachine()
    trail = walk(m, "perceived", "decided", "act_now", "acted", "continue_run",
                 "perceived", "decided", "act_now", "acted")
    assert trail == ["see", "decide", "gate", "act", "verify", "see",
                     "decide", "gate", "act", "verify"]


def test_idle_path_skips_act():
    m = RunMachine()
    trail = walk(m, "perceived", "decided", "idle_now", "continue_run")
    assert trail == ["see", "decide", "gate", "verify", "see"]


def test_heal_cycle_returns_to_act_then_verify():
    m = RunMachine()
    trail = walk(m, "perceived", "decided", "act_now", "heal_needed",
                 "healed", "acted", "continue_run")
    assert trail == ["see", "decide", "gate", "act", "heal", "act",
                     "verify", "see"]


def test_heal_gives_up_to_verify():
    m = RunMachine()
    trail = walk(m, "perceived", "decided", "act_now", "heal_needed",
                 "heal_failed")
    assert trail[-1] == "verify"


def test_done_and_stopped_terminals():
    m = RunMachine()
    walk(m, "perceived", "decided", "finish")
    assert state_id(m) == "done"
    m2 = RunMachine()
    walk(m2, "perceived", "decided", "idle_now", "abort")
    assert state_id(m2) == "stopped"


def test_starts_at_see_and_rejects_jumps():
    m = RunMachine()
    assert state_id(m) == "see"
    with pytest.raises(TransitionNotAllowed):
        m.acted()  # see -> act skips decide+gate
    with pytest.raises(TransitionNotAllowed):
        m.healed()  # heal only reachable from acting


def test_terminals_have_no_outgoing():
    m = RunMachine()
    walk(m, "perceived", "decided", "finish")
    with pytest.raises(TransitionNotAllowed):
        m.continue_run()
    for ev in ("perceived", "decided", "act_now", "idle_now", "finish",
               "abort", "acted", "heal_needed", "healed", "heal_failed",
               "continue_run"):
        with pytest.raises((TransitionNotAllowed, AttributeError)):
            getattr(m, ev)()


def test_state_ids_match_transcript_vocabulary():
    m = RunMachine()
    assert state_id(m) == "see"
    m.perceived()
    assert state_id(m) == "decide"
    m.decided()
    assert state_id(m) == "gate"


def test_unknown_event_is_loud():
    m = RunMachine()
    with pytest.raises(AttributeError):
        advance(m, [], "teleport")


def test_full_edge_table_parity():
    """Every legal edge fires; every other pair raises. Mirrors EDGES
    plus the heal cycle from src/machine/run_machine.ts."""
    legal = {
        ("see", "perceived", "decide"),
        ("decide", "decided", "gate"),
        ("gate", "act_now", "act"),
        ("gate", "idle_now", "verify"),
        ("gate", "finish", "done"),
        ("gate", "abort", "stopped"),
        ("act", "acted", "verify"),
        ("act", "heal_needed", "heal"),
        ("heal", "healed", "act"),
        ("heal", "heal_failed", "verify"),
        ("verify", "continue_run", "see"),
        ("verify", "finish", "done"),
        ("verify", "abort", "stopped"),
        ("see", "abort", "stopped"),
    }
    states = ["see", "decide", "gate", "act", "verify", "heal", "done", "stopped"]
    events = ["perceived", "decided", "act_now", "idle_now", "finish", "abort",
              "acted", "heal_needed", "healed", "heal_failed", "continue_run"]
    for src in states:
        for ev in events:
            m = RunMachine()
            # drive machine to src state first
            _drive_to(m, src)
            assert state_id(m) == src
            try:
                getattr(m, ev)()
                got = state_id(m)
                assert (src, ev, got) in legal, f"unexpected legal edge {src} -{ev}-> {got}"
            except TransitionNotAllowed:
                assert not any(s == src and e == ev for s, e, _ in legal), \
                    f"missing legal edge {src} -{ev}-> ?"


def _drive_to(m, target):
    paths = {
        "see": [],
        "decide": ["perceived"],
        "gate": ["perceived", "decided"],
        "act": ["perceived", "decided", "act_now"],
        "verify": ["perceived", "decided", "idle_now"],
        "heal": ["perceived", "decided", "act_now", "heal_needed"],
        "done": ["perceived", "decided", "finish"],
        "stopped": ["perceived", "decided", "idle_now", "abort"],
    }
    for ev in paths[target]:
        getattr(m, ev)()

def test_edges_table_matches_machine():
    """EDGES (the table phase_step validates against) agrees with the
    machine on every state/event pair - one source of transition truth."""
    from src.machine.run_engine import EDGES, legal
    states = ["see", "decide", "gate", "act", "verify", "heal", "done", "stopped"]
    events = ["perceived", "decided", "act_now", "idle_now", "finish", "abort",
              "acted", "heal_needed", "healed", "heal_failed", "continue_run"]
    assert legal("", "see") and not legal("", "decide")
    for src in states:
        for ev in events:
            m = RunMachine()
            _drive_to(m, src)
            try:
                getattr(m, ev)()
                assert legal(src, state_id(m)), f"machine allows {src} -{ev}-> but EDGES forbids"
            except TransitionNotAllowed:
                pass
    for src, dsts in EDGES.items():
        for dst in dsts:
            assert dst in states and src in states
