---
name: open-typesafe-camoufox
description: Drive a headed Camoufox browser from plain English with the otc CLI (uv run otc.py). Use when asked to run a browser task, check or replay a run, preflight keys and browser, or work inside the open-typesafe-camoufox repo.
---

# open-typesafe-camoufox (`otc`)

CLI tool that drives a headed Camoufox browser toward a goal typed in plain
English (~$0.0002/step). It never sends screenshots to a big model: Jev (TypeSafe
System One) classifies one action per step, a small writer model composes free
text only when a field genuinely needs it.

Repo: `open-typesafe-camoufox/`. Call shape from the repo root:

```bash
uv run otc.py --url <start-url> --task "<plain-English goal>" --budget 240 --max-steps 20
```

Runtime rules: `docs/LIBRARIES.md` · State machine rules: `docs/STATEMACHINE_CONVENTIONS.md` · Ref dictionary: `docs/REF_TYPES.md`

## Setup (once)

```bash
uv sync
cp .env.example .env.local   # fill GROQ_API_KEY + TYPESAFE_API_KEY
uv run otc.py --url https://example.com --preflight   # proves keys + headed browser
```

Writer needs no new key (small Groq model; `WRITER_MODEL` overrides,
`ANTHROPIC_API_KEY` optionally selects haiku).

## Commands

```bash
# solve a task (Jev-primary decide loop)
uv run otc.py --url https://www.google.com --task "Type ONE surprising moon fact in the search box, press Enter, read the top result, and mark done." --budget 240 --max-steps 20
# sign-in style task: credentials ONLY via {ENV} placeholders (values never log)
uv run otc.py --url https://x.com/login --task "Sign in with {TWITTER_USERNAME} / {TWITTER_PASSWORD}" --budget 120
# flags: --headless --fps 2-5 --min-confidence 0.4 --steer steer.txt --verbose
# fallback: --legacy-planner (old GPT planner loop)
# offline replay, no browser:
uv run otc.py --replay runs/<timestamp> --replay-step 3
uv run otc.py --replay runs/<timestamp>   # list saved steps
# quick demo (same shape as the CLI call above):
uv run python run_fact.py
# tests:
uv run python -m pytest -q
```

Steer a live run from another terminal via `steer.txt`:
`stop` | `goto <url>` | `pause` | `resume` | any instruction line.

## Reading a run

Every run writes `runs/<timestamp>/`:

| file | use |
|---|---|
| `run.log`, `run.json` | full feed; goal, outcome, seconds, config |
| `step-NN-raw.png` | screenshot capture |
| `step-NN.png` | elements blue, chosen red, focused field green |
| `step-NN-payload.jsonl` | pydantic-validated JSON line: state + questions + rankings + decision |
| `step-NN-answers.json` | every classifier probability (debug stalls here first) |
| `transcript.jsonl`, `cursor.json` | per-step lines + cursor trail |

Live feed lines: `SEE` (elements/links/tabs/focused/text) → `PROPOSE <kind> [#item] : <rationale>` → `DECIDE <kind> conf=<x> [item=#n] | ready=<x> text?=<x> done?=<x> prog=<x> appr=<x>` → `ACT` → `RESULT` → `TIME propose=<s> decide=<s> act=<s>`. `kind` is one of `wait | click_item | type_at | press_enter | refresh | close_others | goto | challenge | done | none` (each with a written what/not-for boundary). At approval ≥ 0.7 the proposal executes over the Choice vote (`[approved-override]`, `+ newtab <url>` when a tab opens). The Noul flags ride along: `ready` can only add patience, `text?` gates the writer call, `done?` must agree before `done` is accepted. `conf < --min-confidence` idles;
two doubt no-ops stop the run; `wait` is patience (loading page) and never
stops it; `done` is accepted only on a settled page with real text.
Clicks auto-adopt new tabs (`+ newtab <url>` in RESULT, slow popups at next
SEE); a tab switch logs `TAB SWITCH` with a fresh DOM digest and starts a
reading cooldown (`goto refused until step N`). `close_others`
always keeps the current tab and its opener. Every step walks
`see → decide → gate → [act] → verify` (transcript `phases`; a stale/covered click detours act-heal-act once via Jev triage (remap/dismiss/challenge/gated synthesis/abort); `NOEFFECT` means a settled page identical to the last step, twice ends the run.

## Primitive / LLM contract (how decisions become actions, exactly)

Three layers, strict direction. Camoufox primitives never see model output
directly; LLM capabilities never touch the browser directly; the harness in
`src/runner.py` enforces every crossing:

1. **LLM proposes + classifies.** Writer proposes one action (`PROPOSE`, with
   rationale + refs from the current map). Jev classifies kind/item/site
   (Choice), flags (Nouls: `ready`/`text?`/`done?`/`fit`/`approve`), progress
   (Score). All state Jev sees is in the packet: elements (ref/kind/label/
   text/href/host/region/box), focused field, page text, tabs, notes,
   visited, lessons. Nothing else exists for it.
2. **Harness routes.** `resolve_proposal_action` picks override / fallback /
   mismatch-idle / none. Gate (`conf`), loop-guard (3× identical intents),
   Noul gates, veto (bare input clicks), stale-map check (live kind must
   match decided kind), point check (`elementFromPoint` must hit target or
   clickable cover) run in order; the first that fires wins.
3. **Primitives execute against the live DOM only.** `click_item`/`type_at`/
   `goto_url`/etc. in `src/actions.py` re-measure the box, verify the point,
   then dispatch — never on stale coordinates. Failures return `error:` and
   count as no-ops, never moves.
4. **Stuck escalation.** An identical mismatch 3× in a row (`MISMATCH x3`)
   escalates once per situation to a writer-proposed destination goto
   (`ESCALATE goto <url>`); with no fresh destination it counts no-ops and
   the run stops honestly. The loop can idle on patience (`wait`,
   cooldowns) but never on a known-dead pick.

## Caveats (do not work around these)

- Headed by default: hands off mouse/keyboard/focus while it runs.
- Without `TYPESAFE_API_KEY`, grounding goes blind (`conf=0.00` heuristic).
- DOM element-map targeting only; canvas/icon-only controls are invisible.
- Slow pages burn steps on `wait`; raise `--budget`/`--max-steps`.
- The writer never invents credentials; `{ENV}` placeholders must exist in
  `.env.local` or credential steps are skipped.
- Never commit `.env.local` or `runs/` (both git-ignored).
