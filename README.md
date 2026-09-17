# open-typesafe-camoufox

A CLI tool that drives a headed Camoufox browser toward a goal you type in
plain English — for about $0.0002 a step. It never sends a screenshot to a
big model. Instead it reads the page deterministically, asks a small
classifier which action comes next, and only calls a writing model when a
text field genuinely needs free text.

```bash
otc --url https://www.google.com --task "Type ONE surprising moon fact in the search box, press Enter, read the top result, and mark done."
```

(`otc` here is `uv run otc.py` from a source checkout — see Install. When
packaged, `pyproject [project.scripts]` installs real `otc` /
`open-typesafe-camoufox` commands.)

Shaped after [`typesafe-computer-use`](https://github.com/awlevin/typesafe-computer-use);
CLI ergonomics take a leaf from [`camoufox-cli`](https://github.com/Bin-Huang/camoufox-cli)
(one installable command, flags-over-config, offline replay).

## Goal

Make browser computer-use **just work** on Camoufox: cheap enough to run
all day, reliable enough to trust with real tasks (sign-in, search, read),
and observable enough to debug offline when it stalls. The project started
as `open-jev-solver` (a 2-tool pydantic harness) and was refactored into
this shape once live runs showed the costs clearly.

## Goal: earn $1 USDC on Solana

The standing end-to-end target: **earn $1 USDC on the Solana chain** to
wallet `d8XdEYRHvti6WZNEuohJqENHVGVwMxS9F1PXg5kxBMo`.

Stated honestly: `otc` has no wallet, no accounts, and no payment rails —
it cannot earn or move crypto by itself, and bot-checks/CAPTCHAs are stop
conditions, not puzzles to defeat. What the loop *can* do, recursively
until it works, is the research leg: find legitimate beginner routes to
$1 USDC on Solana (faucets, learn-to-earn, microtasks, freelance gigs paid
in crypto), open the most promising results, read pay rates/requirements/
payout methods, and report the top options with a note quoting each page.

Judgment criteria for a run (in order):
1. `done=True` with a note naming concrete routes + amounts — complete.
2. Sustained multi-page progress (25 steps, new tabs adopted, notes
   banked on distinct URLs) without ever re-clicking a dead target.
3. Anything else is a stall: fix the decision, not the step count.

## What we did here

1. **Speed + consistency.** Profiled the cursor and found each humanized
   dispatch cost ~1.35s and gestures stacked wobble on wobble (model curve
   + radius jitter + per-dispatch bezier). Now: one `HUMANIZE_LEVEL = 0.3`
   constant (~0.35s/dispatch), 1–2 dispatches per move, one clean 5-hop
   ellipse highlight. Steps went from ~17–19s to ~4–6s.
2. **Clear-before-type.** The planner typed into already-filled inputs and
   the query concatenated every step. Every `type` now selects-all and
   deletes first, so typing always replaces.
3. **The typesafe flip.** The big planner LLM did the real per-step work
   while Jev was a `conf=0.00` braille hint nobody used — the most
   expensive possible arrangement. Inverted: **Jev classifies
   (kind/item/site) over the DOM element map with confidence gating; a
   small writer model composes free text only.** Verified live:
   `type_at 0.97` → `press_enter 0.88` → `wait` (loading, neutral — no
   retype, no accumulation) → `done`, 4 steps, `done=True`.
4. **One platform adapter + replay parity.** Collapsed two capability
   classes and deleted the dead video-agent verbs/schemas. Every step
   writes raw + annotated screenshots, the exact Jev payload, and the full
   probability answers — stalls replay offline with `--replay`.

## Why this decision-making shape

Frontier-model computer use ships a screenshot and waits seconds for a
plan, every step. Most steps don't need a plan — they need one choice from
a short, mutually exclusive list, with a confidence number to gate on.
[TypeSafe](https://docs.typesafe.ai) (Jev, "System One") is best-in-class
for exactly that: a decision model answering `Choice` over up to 255
options with a full probability distribution and calibrated confidence, in
a few hundred milliseconds, with free output tokens. So the loop is built
around it: Jev picks *what to do*, a small writer model only writes *words*
when a field genuinely needs them, and code revalidates everything
(URLs must be clean https, credentials are never free-typed, secrets
resolve from `{ENV}` placeholders at execution and never enter prompts).

## Install

Python 3.11+, [uv](https://docs.astral.sh/uv/).

```bash
git clone <this-repo> open-typesafe-camoufox
cd open-typesafe-camoufox
uv sync
cp .env.example .env.local   # fill GROQ_API_KEY + TYPESAFE_API_KEY
uv run otc.py --url https://example.com --preflight   # proves keys + headed browser
```

`uv run otc.py` is the call shape (a source checkout, like
`camoufox-cli`'s daemon model but without the install step). After
`pip install .`, the `[project.scripts]` entry points give you bare `otc`.
No new keys needed for the writer — it defaults to
a small Groq model (`WRITER_MODEL` overrides; `ANTHROPIC_API_KEY` is an
optional haiku override).

## Use

```bash
uv run otc.py --url https://www.google.com --task "Type ONE surprising moon fact in the search box, press Enter, read the top result, and mark done." --budget 240 --max-steps 20
uv run otc.py --url https://x.com/login --task "Sign in with {TWITTER_USERNAME} / {TWITTER_PASSWORD}" --budget 120
uv run otc.py --url https://example.com --preflight
uv run otc.py --replay runs/<timestamp> --replay-step 3   # offline, no browser
uv run otc.py --url ... --task ... --legacy-planner       # old GPT planner loop (fallback)
```

While a run is going, steer it from another terminal via `steer.txt`
(`stop` | `goto <url>` | `pause` | `resume` | any instruction).

## How a step works

```
screenshot ─► element map (DOM probe, reading order, data-jev tags)
              focused field (role/label/placeholder/value, credential flag)
              page text (visible words — the reading ground truth)
                          │
                          ▼
             Groq proposes the single best action as a yes/no question
                          │
                          ▼
             one Jev request: Choices, Noul flags, and a Score
             ┌───────────────────────────────────────────────┐
             │ kind : wait | click_item | type_at |          │
             │        press_enter | refresh | close_others | │
             │        goto | done | none                     │
             │        (each with a what/not-for boundary)    │
             │ item : which element idx — structured options │
             │        (label/text/href/fill-state/selector)  │
             │ site : which catalog URL (goto targets)       │
             │ Noul : page_ready? needs_text? task_done?     │
             │ Noul : approval? (LLM-posed question)         │
             │ Score: progress on the task spectrum          │
             └───────────────────────────────────────────────┘
                          │  conf < 0.4 → idle (no-op)
                          │  two doubt no-ops → stop
                          ▼
             deterministic action ─► verify ─► next step
```

`wait` is patience, not doubt: a loading page idles without counting
toward the stop rule, so the loop can never retype into a transition (the
old query-accumulation bug is structurally impossible). `done` is accepted
only on a settled page with real text. Clicks that open a new tab are
auto-adopted (harvest old buffer, bind the new page, restart the tracker)
so the next step reads the new content instead of re-clicking; `refresh`
reloads a stale tab and `close_others` trims tabs while always keeping the
current tab and its opener (a click-opened popup dies with its opener in
this build — verified live). Nav listeners are generation-guarded: after an
adoption, events from stale tabs are ignored so a dead tab can't clobber
the URL bookkeeping or fire tracker recovery on the wrong tab. A tab switch starts a 4-step reading cooldown:
the fresh tab's DOM is recaptured with a digest immediately, `goto` is
refused until the cooldown ends, and the proposer is told to read the
adopted page instead of navigating away.

Each step walks an explicit phase machine —
`see → decide → gate → [act] → verify`, ending `done`/`stopped`
(idle paths skip `act`; illegal transitions raise instead of drifting).
`verify` fingerprints the outcome: a settled page identical to the last
step means the action changed nothing, twice in a row ends the run
honestly. The three Noul flags ride along: `page_ready` can only add
patience (never override the loading check), `needs_text` gates the
writer call, `task_done` must agree before `done` is accepted. `progress`
scores the run on the task-completion spectrum for long-horizon tracking.

## What fires when (capability trigger map)

Decide path (default, `otc --url … --task …`):

| phase | module | fires |
|---|---|---|
| see | `perception` | element map, focused field, page text, tabs; banks notes + visited |
| decide | `decide` (Jev) | kind/item/site Choices + page_ready/needs_text/task_done/approval Nouls + progress Score |
| propose | `writer` (Groq) | reads all elements + URLs, poses the single best action as the approval question |
| gate | `runner` | confidence gate, loop-guard, Noul gates |
| act | `actions` → `platform` | click/type/enter/refresh/goto/close; auto-adopts new tabs |
| act | `writer` | only on `type_at` (non-credential) and off-catalog `goto` |
| verify | `runner` | fingerprint vs last step, no-op and dead-run stops |

Legacy path (`--legacy-planner`): `read_frame` → braille → `decide_cursor`
hint; `build_step_agent` planner → `JevStep`; `JevCapability.move_cursor`
executes. Uses `jev_agent`, `jev_tools`, `jev_actions`, `agent_runner`.
Shared by both: the `platform` adapter, tracker, human motion, run folder.

## Run folder

Every run writes `runs/<timestamp>/`:

| file | contents |
|---|---|
| `run.log`, `run.json` | everything printed; goal, outcome, seconds, config |
| `step-NN-raw.png` | the screenshot capture |
| `step-NN.png` | elements numbered blue, chosen red, focused field green |
| `step-NN-payload.txt` | exact state + criteria sent to Jev, then the decision |
| `step-NN-answers.json` | every probability the classifier returned |
| `transcript.jsonl`, `cursor.json` | per-step lines + cursor trail |

## Caveats

- **Headed by default** (`--headless` opts out): watch the window and keep
  hands off mouse/keyboard/focus while it runs — a background idle-motion
  loop keeps the cursor alive and fights you for it.
- Needs `GROQ_API_KEY` + `TYPESAFE_API_KEY`; without the latter Jev falls
  back to a heuristic and grounding goes blind (`conf=0.00`).
- Element-map targeting only sees the DOM — canvas/icon-only controls are
  invisible to it (pixel fallback is future work).
- Slow pages burn steps on `wait`; raise `--budget`/`--max-steps` for them.
- Passwords are only filled from `{ENV}` placeholders declared in `--task`
  (see `.env.example`); the writer will never invent credentials.
- `runs/` and `.env.local` are git-ignored; never commit secrets.

## Layout

```
src/
  browser/camoufox.py   the ONLY module touching playwright/camoufox
  perception.py         element map, focused field, page text, state packet
  decide.py             Jev primary selector (kind/item/site + confidence)
  writer.py             small-model free text ({fill,text}/{ok,url})
  actions.py            one handler per kind -> history line
  runner.py             Jev-primary step loop, stop rules, steer
  report.py             run folder, annotated PNGs, payload, answers, replay
  deps.py               typed RunState (replaces __jev_* string tuples)
  run/                  CLI (run.py: otc), env_loader, logging_utils
  run/agent_runner.py   legacy GPT planner loop (--legacy-planner only)
```

## Development

```bash
uv run python -m pytest -q
```

Git hooks (Jev-backed, see `scripts/jev_hooks.py` + `docs/LIBRARIES.md`):

```powershell
scripts/install-hooks.ps1   # pre-commit maps staged files, pre-push ranks by issues
```

- `pre-commit` classifies/maps staged `.py` files (advisory; `--strict` to block).
- `pre-push` ranks pushed `.py` files by Jev issue score and blocks on
  ≥ 0.75. Bypass with `git push --no-verify` or `JEV_HOOKS_OFF=1`.
- No key / no network → heuristic fallback, never blocks.
