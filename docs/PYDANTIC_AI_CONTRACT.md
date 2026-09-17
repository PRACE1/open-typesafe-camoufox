---
title: "Pydantic AI Contract (harness source of truth)"
tags: [pydantic-ai, contract, retries, recovery, toolsets]
status: active
created: 2026-09-18
---

# Pydantic AI contract — source of truth for harness behavior

Fetched 2026-09-18 from `https://ai.pydantic.dev/{api/exceptions,toolsets,tools,tools-toolsets/tools-advanced,agent,graph,graph/beta/decisions,api/usage}`.
Rule: where this doc quotes behavior, the quoted docs govern — never invent
APIs, classes, retry semantics, or lifecycle guarantees. Where the harness
differs, the divergence is marked `[DIVERGENCE]` with its reason.

## 0. Scope honesty: what the library does here

`pydantic-ai>=1.0` is a declared dependency, but the Jev-primary loop is
CUSTOM: `src/runner.py` drives; Jev is a classifier API (Choice/Noul/Score),
not a chat model; no `Agent`/`Graph` objects execute. Actually imported
today: `RunContext` (legacy `jev_actions.py` only) and `BaseModel`
(schemas). Everything below is ADOPTED PATTERN, not delegated execution —
the single exception is the pydantic validation library itself.

## 1. Retry vs terminal failure (exceptions API)

Documented: raise `ModelRetry` from tools/validators/hooks to send a
`RetryPromptPart` back — the model corrects arguments, picks another tool,
or tries a different approach. Raise `ToolFailed` for a complete-but-failed
call (missing resource, unsupported op, definitive upstream error): recorded
as a failed `ToolReturnPart` (`outcome='failed'`), the model adapts, and it
does NOT consume the per-tool retry budget. Bound repeats with
`UsageLimits` — `request_limit` counts every call, `tool_calls_limit`
counts successful calls only. Any other exception propagates out of the run.

Harness mapping: `error:`-prefixed results are `ToolFailed` (model sees via
history, adapts, budget untouched). Heal re-attempts are `ModelRetry` —
always with FRESH re-perception first (re-probe, re-sample, never a blind
repeat), exactly one re-attempt per failure. Per-tool retry budget = 1
re-attempt `[DIVERGENCE: docs default to layered N; ours is fixed at one
because a second identical dispatch on a live page is either a duplicate
side effect or proof the context is misunderstood]`.

## 2. Tool execution, validation, retries, timeouts

Documented: arguments validated via Pydantic → `ValidationError` auto-creates
a retry prompt. Precedence: per-tool `Tool(max_retries)` > per-toolset >
override block > per-run arg > per-run spec > agent default (`1` built-in).
Exhaustion raises `UnexpectedModelBehavior`. Tool timeout = retryable
failure + retry prompt, counts toward the limit. Parallel tool calls run via
`asyncio.create_task` in emission order. `args_validator` runs after schema
validation; may raise `ModelRetry`/`ToolFailed`/defer. `prepare`/`prepare_tools`
reshape definitions per step.

Harness mapping: `KIND_CRITERIA` what/not-for + `fits` Noul = argument
validation (mismatch-idle/veto = validation retry prompt; nothing dispatches
on invalid args). Writer JSON parsed strictly; garbage → decline, never guess.
Timeouts via `asyncio.wait_for` on every platform call. `[DIVERGENCE: one
action per step, never parallel — the humanized cursor is a single serial
actuator; concurrent dispatches would fight for one mouse.]`
`validator.py` = args_validator + execution hooks for synthesized code.
Proposal approval (writer poses, Jev Noul approves) mirrors
`ApprovalRequiredToolset` (pose-then-approve before execution).

## 3. Toolsets

Documented: `FunctionToolset` (`tool`/`tool_plain`/`add_function`/`add_tool` —
including registering NEW tools during a run for future steps), toolset
`instructions` appended after agent instructions, `Filtered`/`Combined`/
`Prefixed`/`Renamed`/`Prepared` composition, `WrapperToolset.call_tool`
execution override, `DynamicToolset` rebuilt per step, `DeferredLoading`,
`ExternalToolset` + `DeferredToolRequests/Results`, `AbstractToolset`
lifecycle (`for_run`/`for_run_step`/`__aenter__`/`__aexit__`,
`get_instructions`).

Harness mapping: `actions.py` verbs = the toolset; criteria = tool
definitions; `dynamic_registry.register_capability` = `add_function`
during a run (visible next step); `StepPayload`/packet = `RunContext`
(read-only task/history/notes); `MEMORY.md` lessons = toolset instructions;
per-probe ref scoping ≈ per-step `PreparedToolset` filtering (128 cap).

## 4. Agents and execution

Documented: `run`/`run_sync`/`run_stream`/`iter` traverse the graph;
`End` ends the run; `message_history` resumes; `RunCancelled.all_messages()`
resumes after cancel; `CancellationToken` is single-use; `end_strategy`
governs dangling tools; `CustomEvent` (app-owned) vs `CapabilityEvent`
(namespaced).

Harness mapping: `run_decide_session` ≈ `Agent.run`; mission driver ≈
resume-with-history across sessions (`seed_visited` + steer deltas);
`transcript.jsonl` + run `MEMORY.md` ≈ message history (`--replay` reads);
steer `stop` ≈ cancellation token (operator-owned); `phases` trail ≈ node
iteration log. `[DIVERGENCE: feed lines are logs, not an event stream;
no durable execution; our End = done/stopped terminals.]`

## 5. Usage limits

Documented: `UsageLimits` — `request_limit` default 50 checked BEFORE each
request; token limits checked AFTER each response; `tool_calls_limit` counts
successful calls only; `cost_limit`; per-request input caps;
`check_before_request` / `check_tokens` / `check_before_tool_call` raise
`UsageLimitExceeded`. `RunUsage` sums requests/tool_calls/tokens/cost.

Harness mapping: `request_limit: 50` ≈ `max_steps` default 50;
`check_before_request` ≈ loop-top budget/step check; per-step accounting ≈
after-response checks; `moves` ≈ `tool_calls` (successful dispatches only —
noops excluded, same rule); wall-clock `budget_s` ≈ cost-limit analog IN
TIME, not dollars `[DIVERGENCE: our dollar cost is fixed (~$0.0002/step) by
model choice, so budget is enforced in seconds/steps]`. `stop_limits()`
scales rails to `max_steps`. Rule: limits are TERMINAL, never complete —
`stop_reason` recorded; `done` requires a verified outcome.

## 6. Graph execution, state, decisions

Documented: `BaseNode.run` return annotations ARE the edges (enforced at
runtime); state via `GraphRunContext` mutated along the run; decision nodes
(`g.decision`/`g.match`, first match wins: type/literal/predicate +
catch-all); mermaid `render()`; `iter()` for manual driving.

Harness mapping: `RunMachine` states ≈ nodes; `advance()` events ≈ edges
(`TransitionNotAllowed` ≈ runtime misbehavior detection); `phases` trail ≈
node log; gate routing + heal triage + recovery selection ≈ decision nodes
(first-match priority, documented order); `docs/run_machine.dot` ≈
`graph.render()` (drift-tested). Kind Choice ≈ tool_choice auto; approval
override ≈ per-step model-settings override.

## 7. Outcome taxonomy (normative)

- `complete`: DONE accepted on settled page + real text + `task_done>=0.5`
  with the outcome assembled from read notes. Verified FROM BROWSER STATE;
  tool success alone is never sufficient.
- `progress`: acted with observable effect (`moves++`, fingerprint changed).
  Necessary, never sufficient, for completion.
- `noop`: idle/wait/gate/lowconf/mismatch-wait with no dispatch. Triggers
  re-perception + the recovery cycle below. Never terminal by itself, never
  completion.
- `recoverable`: failed action with a live strategy (heal triage, challenge
  work, refresh/escape/back compensation, synthesis). Bounded attempts, each
  audited (observed, reason, strategy, action, result, next).
- `unresolved`: strategies exhausted, budgets hold — the loop and the
  mission driver continue; never reported as success.
- `terminal`: done + honest stops (noops/dead/fixation/captcha-cap/
  budget/steps/steer-stop/empty-session/session-cap), each with a reason.
  Budgets are terminal, never complete.

Recorded per step as `entry["verdict"]`; per run in the summary.

## 8. Recovery cycle (normative)

Every noop step with budget left runs observe → classify → select →
execute → reread → verify → continue INSIDE the verify phase as engine
state `recover` (`verify→recover→see`):

1. observe: the step's SEE already re-read the page (never act on stale context).
2. classify: `classify_noop` maps the step record to lowconf / fixation /
   mismatch / notready / cooldown / idle / failed / blocked / unknown.
3. select: `select_recovery` first-match: blank→refresh, failed-same-print→
   refresh, fixation→refresh, blocked→None (challenge flow owns it),
   mismatch→None (escalation owns it), else None. Returns one of
   refresh/escape/back/None.
4. execute: one compensating dispatch with settle + snapshot (refresh via
   platform, escape via key, back via history).
5. reread: next SEE re-reads (plus immediate snapshot digest).
6. verify: fingerprint vs pre-recovery print; `acted=True` so dead-run
   detection judges the compensation honestly.
7. continue: `recovered` → see; `recoveries` bounded by
   `max(3, max_steps//100)`; every attempt audited in `entry["recover"]` +
   history + RECOVER log + summary list.

A retry NEVER repeats the same action: it re-observes first, then compensates.

## 9. Capability expansion (normative)

Mirrors `add_function`-during-run + `args_validator` + execution hooks:
synthesize inside a bounded envelope (failure trace, ref slice, skeleton,
toolbox, invariants) → AST gate (asyncio-only imports, single `execute`,
no eval/exec/open/os/sys/subprocess) → signature gate
(`(platform, ref, ...)`) → 3s shadow dry-run (must return str, must not
raise; probes resolvability, not side-effect-freedom — documented limit) →
`register_capability(heal_step<N>)` → execute once with 30s timeout →
audit source to run dir. Any gate failure = `heal_failed`, never registered,
never executed.

## 10. Limitations (do not architect on assumptions)

- No vision model in the stack: image grids are unsolvable; bounded
  attempts (`CAPTCHA_MAX_ATTEMPTS`) then honest stop.
- Snapshot `: value` suffixes are unreliable on some pages → live
  `read_input_value` backup for the retype guard.
- Aria refs die on navigation; `fN` frame prefixes vary per load → never
  single-tier trust (aria → selector → nth), never cross-load refs.
- Jev is a classifier (Choice/Noul/Score), not a chat model: it votes, it
  cannot explain. All rationale comes from writer/proposer + history.
- `pydantic_ai.Agent`/`Graph` do not execute this loop; only the patterns
  above are adopted. `RunContext` import survives in legacy code only.
