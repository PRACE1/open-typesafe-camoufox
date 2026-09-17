---
title: "State Machine Conventions (python-statemachine)"
tags: [statemachine, conventions, run-loop]
status: active
created: 2026-09-17
---

# State machine conventions (python-statemachine)

Pinned subset of the upstream
[AGENTS.md](https://raw.githubusercontent.com/fgmacedo/python-statemachine/develop/AGENTS.md)
that every change to `src/machine/run_engine.py` and its callers must follow.
The TypeScript twin is the `xstate-v5` skill; concepts translate, primitives
do not — use this file for Python, that skill for TypeScript.

## Declare, don't script

- States and events are descriptors: `State`, named events via `s1.to(s2)`.
- Compose alternative targets with `|`: `heal.to(act, cond="x") | heal.to(verify)`.
- `src/machine/run_engine.py::EDGES` is the single transition truth table;
  `legal()` and `advance()` are the only emitters. `tests/test_machine.py`
  proves the machine matches the table on every state/event pair.

## Telemetry via callbacks, not prints

- `on_enter_<state>` / `on_exit_<state>` for state-entry side effects
  (e.g. `on_enter_heal`: bump `heal_attempts`, structured log line).
- `after_<event>` / `after_transition` for finalize hooks (run on success
  and failure paths alike).
- The runner loop body stays free of ad-hoc phase logging; anything the
  transcript needs comes through the engine.

## Guards decide, callers inform

- Recovery preconditions are `cond=` guards on the transition
  (e.g. `healed_target_ready`), never `if` statements scattered in the
  runner. An unmet guard raises `TransitionNotAllowed`: fail loud, never
  drift.
- Guards read machine attributes with `getattr(self, name, False)` defaults
  so a fresh machine has deterministic closed-guard behavior; tests that
  fire guarded events set the flag first.

## Diagrams are generated, never drawn

- `contrib/diagram.py` exports the machine to `docs/`; a test asserts the
  committed file matches, so mermaid drift is impossible. Requires `pydot`.

## Errors are events, not exceptions in the loop

- `StateMachine` has `catch_errors_as_events=False`: action code keeps its
  own fail-soft `error:`-prefix convention (`src/actions.py::action_failed`).
- The heal cycle (`act -> heal -> act/verify`) is the declared error path;
  nothing catches-and-continues around `advance()`.

## Test and style discipline

- TDD: every behavior lands with its test in the same commit; 100% branch
  coverage on touched modules is the bar (`--cov --cov-branch`).
- `ruff` (line length 99), Google-style docstrings (document only the
  non-obvious), top-level imports (lazy only to break cycles).
- Commits are signed Conventional Commits; pre-commit hooks are never
  bypassed — a hook failure is fixed at root cause.
