# Libraries in use (and how we use them)

Saved reference for code-smell / best-practice checks (see also the Jev
`pre-push` hook, which ranks changed files against these notes). Every claim
below is grounded in this repo — file paths given.

## TypeSafe / Jev (`TYPESAFE_API_KEY`, `TYPESAFE_BASE_URL`, `TYPESAFE_MODEL`)

- Decision model, not a chat model. One POST to `{base}/systemone` with
  `{"model", "state", "questions"}`; see `src/decide.py: _env`, `_post`.
- Three primitives, used exactly as documented at https://docs.typesafe.ai:
  - **Choice** (≤255 options): `kind` (verb), `item` (element idx), `site`
    (URL catalog). Options carry structured criteria objects
    (`what`/`not_for` boundaries), per the structured-questions guidance.
  - **Noul** (yes/no probability, no separate confidence): `page_ready`,
    `needs_text`, `task_done`, `fits`, `approval`.
  - **Score** (0..1 spectrum): `progress`.
- Answers parsed defensively with defaults (`src/decide.py: _decode`,
  `_noul`); missing keys must never crash the loop.
- Cost/latency envelope (per typesafe-computer-use measurements):
  ~$0.0002/decision, 130–380ms. Never send screenshots to a big model.
- Smells to flag: free-text generation from Jev (it can't — route text
  through the writer), unbounded Choice sets (>255), overlapping options
  (reads as doubt, tanks confidence), ignoring `confidence` on gates.

## Camoufox + Playwright (`camoufox`, `playwright`)

- Single adapter: `src/browser/camoufox.py` (`CamoufoxPlatform`) is the ONLY
  module that touches `page`/Playwright. Everything above it is
  platform-blind — keep it that way.
- `humanize` accepts bool|float; we pin `HUMANIZE_LEVEL = 0.3`
  (`src/capability/human_move.py`) ≈ 0.35s/dispatch.
- Cursor truth lives in the browser (`window.__cursorTracker`, injected via
  `set_page`/`reinject_tracker`); Python keeps an accumulated buffer across
  navigations (`_harvest_and_accumulate` before every nav).
- Navigation must go through `safe_goto` (never raw `page.goto`) or the
  cursor buffer dies with the document.
- New tabs: adopt via `adopt_new_tab` (harvest → bind → re-register
  listeners → restart tracker); never close a tab's opener (popups die
  with it — verified live); listeners ignore events from non-bound pages
  (generation guard).
- Smells to flag: direct `page.mouse/keyboard/goto` outside the adapter,
  missing pre-nav harvest, `humanize=True` (full-cost) instead of the
  constant, sleeps standing in for settle polling (`settle_after_action`).

## pydantic / pydantic-ai

- `JevStep` / action schemas are strict `BaseModel`s (`jev_tools.py`,
  `src/runner.py` state). Structured output keeps the loop parseable —
  never parse model prose with regexes when a schema exists.
- The legacy planner path (`--legacy-planner`) is the only remaining
  chat-model loop; the decide path uses no chat model per step.
- Smells to flag: unvalidated dicts crossing module boundaries, `Any`
  leaking into decision code, exceptions swallowed without a history line.

## python-statemachine

- `RunMachine` (`src/machine/run_engine.py`) owns transition truth; `EDGES`
  + `legal()` + `advance()` are the only emitters. Conventions pinned in
  `docs/STATEMACHINE_CONVENTIONS.md` (callbacks, `cond=` guards, generated
  diagrams, TDD, ruff-99, Google docstrings).
- Heal cycle `act -> heal -> act/verify`; `healed` guarded by
  `healed_target_ready_flag` (remap in hand). `on_enter_heal` counts
  attempts; `after_transition` keeps a bounded forensics log.
- Diagram is generated (`docs/run_machine.dot` via `contrib/diagram`,
  needs `pydot`); a test fails on drift — never hand-edit.
- Smells to flag: phase strings appended outside `advance()`, unguarded
  `healed`, prints in the engine instead of callbacks.

## Ref-based element handles (playwright-cli contract)

- Refs `eN` are ephemeral per-probe handles (`src/capability/element_probe.py`,
  `src/perception.py`); the probe NEVER mutates the DOM (no `setAttribute`,
  no markers) — stealth rule. Python-side `SnapshotElement` equivalents are
  `ElementRef(ref, box)` with viewport-normalized boxes.
- Context control: viewport filter + parent/child 80%-overlap dedup + 128
  cap (inside Jev's 255 ceiling). Jev item Choice keys are refs, decoded via
  `ref_to_idx` (bare ints accepted for cached-prompt compat).
- Resolution re-runs the deterministic probe and takes the nth match,
  cross-checked by kind + label (`_resolve_target`); `generate-locator`
  equivalent is the role/name fallback in `dispatch_verified_click`.
- Smells to flag: any DOM mutation in probe code, selectors stored on
  elements, refs surviving across probes, caps above 255.

## httpx / python-dotenv / Pillow / humanmovemouse

- `httpx.AsyncClient` with explicit timeouts everywhere; 429/529 retried
  with backoff (`src/decide.py`, `src/writer.py`). No bare `requests`.
- `python-dotenv` loads `.env.local` (git-ignored, never committed);
  secrets resolve at execution and log masked (`_mask`).
- `Pillow` renders annotated step PNGs (blue ref boxes / red choice / green
  focus) in `src/report.py`.
- `humanmovemouse` drives the 5-hop ellipse highlight; exactly one clean
  revolution (no stacked wobble sources).
- Smells to flag: network calls without timeouts, secrets in logs/prompts,
  committed `.env.local`, annotations drawn from stale boxes.
