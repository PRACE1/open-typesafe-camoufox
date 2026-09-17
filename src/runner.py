"""
runner.py — the Jev-primary step loop (typesafe's runner.py equivalent).

Each step:
  SEE    screenshot -> perception (element map, focused field, page text)
  DECIDE one Jev request (kind / item / site) + calibrated confidence
  GATE   confidence < min_confidence -> idle (no-op); two consecutive
         no-ops stop the run. A loading page therefore yields wait/none
         instead of another type+Enter — the query can't accumulate.
  ACT    one deterministic handler (actions.py); free text only via
         writer.py; credentials only via {ENV} placeholders from the task.
  REPORT raw + annotated PNG, exact payload, full answers, transcript.

Stop rules: done (validated), steer stop, two consecutive no-ops,
budget, max_steps. Secrets resolve at execution; logs carry masked lengths.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse
from dataclasses import replace
from datetime import datetime
from enum import Enum

from . import perception
from .actions import action_failed, click_item, goto_url, press_key, type_at
from .capability.human_move import HUMANIZE_LEVEL
from .decide import Kind, decide_action
from .deps import RunState
from .perception import is_credential_element
from .writer import ProposedAction, compose_text, propose_action, propose_url, summarize_task

_LOADING_RE = re.compile(r"looking for results|loading|^\\s*$", re.IGNORECASE)


def page_settled(page_text: str) -> bool:
    """False while the page is blank or shows a loading indicator.

    A long page that merely *contains* a banner like "Looking for results
    in English?" (language prompt) alongside real results counts as settled —
    only a short/blank page dominated by the indicator blocks done.
    """
    text = (page_text or "").strip()
    if not text:
        return False
    if len(text) >= 300:
        return True
    return not _LOADING_RE.search(text)


def loop_guard_trip(last_sig: tuple | None, run: int, sig: tuple) -> tuple[bool, int]:
    """True when the same (kind, item, url) repeats 3+ guardable steps in a row.

    The execution layer then forces a wait instead of re-issuing the action:
    fixation breaks mechanically even when the classifier stays confident.
    Waits and other kinds leave the counters untouched, so three identical
    clicks/types trip it even with waits interleaved; any navigation (url in
    the sig) or different target resets the run.
    """
    run = run + 1 if sig == last_sig else 1
    return run >= 3, run


APPROVAL_MIN = 0.5
# Lower bar used ONLY when the Choice pick is vetoed (bare input click):
# a dead-certain dead click loses to an uncertain live proposal.
APPROVAL_FALLBACK = 0.4
OVERRIDABLE_KINDS = ("click_item", "type_at", "goto")
# Bare-clicking a text field never advances anything (no navigation, no state
# change — focusing happens inside type_at). Five straight runs fixated on the
# search box this way, so the runner vetoes it outright.
BARE_CLICK_VETO_KINDS = ("input", "textarea", "select")
# Kinds whose lack of observable effect means failure. type_at is excluded
# on purpose: typing never changes body text, so every type would read as
# "no effect" (repeat-typing is the loop-guard's job, and clear-before-type
# keeps retypes idempotent).
EFFECT_KINDS = ("click_item", "press_enter", "press_escape", "refresh", "back")


# Consecutive doubt no-ops before the run ends. High enough to survive a
# vetoed pick plus one gated wait; low enough that true fixation still ends
# the run instead of burning the whole budget.
STOP_AFTER_NOOPS = 3


def proposal_executable(proposed: ProposedAction | None, elements: list) -> bool:
    """True when an LLM-proposed candidate is safe to execute on approval.

    click_item needs a live, non-input idx (bare input clicks are vetoed
    downstream anyway — rejecting here keeps a misgrounded proposal from
    overriding a sound Choice); type_at needs an input-ish idx; goto needs
    nothing more (a missing URL falls through to the writer-proposal branch).
    All other kinds stay on the Choice path.
    """
    if proposed is None or proposed.kind not in OVERRIDABLE_KINDS:
        return False
    by_idx = {e.idx: e for e in elements}
    if proposed.kind == "click_item":
        return (proposed.item is not None and proposed.item in by_idx
                and by_idx[proposed.item].kind not in BARE_CLICK_VETO_KINDS)
    if proposed.kind == "type_at":
        return (proposed.item is not None and proposed.item in by_idx
                and by_idx[proposed.item].kind in ("input", "textarea", "select"))
    return True  # goto


def should_override(*, approval: float, proposed: ProposedAction | None,
                    choice_kind: str, choice_item: int | None,
                    executable: bool) -> bool:
    """True when an LLM proposal should execute over a differing Choice vote.

    Agreement needs no override (the normal path runs). On disagreement a
    lean-yes (0.5) for the reasoning mind wins: it read every element, host,
    region, and note, while the classifier has a demonstrated fixation mode
    (five runs of search-box clicks at 0.7+). Guard, gate-on-agreement, and
    no-effect rails bound a wrong override to ~one wasted step.
    """
    if proposed is None or not executable:
        return False
    if (proposed.kind, proposed.item) == (choice_kind, choice_item):
        return False
    return approval >= APPROVAL_MIN


def resolve_proposal_action(*, choice_kind: str, choice_item: int | None,
                            elements: list, fits: float,
                            proposed: ProposedAction | None,
                            executable: bool, approval: float) -> str:
    """Route between the Choice vote and the LLM proposal.

    Returns 'override' (disagree + lean-yes: execute proposal),
    'fallback' (Choice pick vetoed/mismatched + viable proposal),
    'mismatch-idle' (vetoed/mismatched with no viable proposal: wait
    neutrally instead of executing known-dead), or 'none'.
    """
    if should_override(approval=approval, proposed=proposed,
                       choice_kind=choice_kind, choice_item=choice_item,
                       executable=executable):
        return "override"
    vetoed = (choice_kind == "click_item"
              and _choice_is_vetoed(elements, choice_item))
    mismatched = (choice_kind in ("click_item", "type_at")
                  and choice_item is not None and fits < 0.4)
    if (vetoed or mismatched) and proposed is not None and executable \
            and approval >= APPROVAL_FALLBACK:
        return "fallback"
    if vetoed or mismatched:
        return "mismatch-idle"
    return "none"


def _choice_is_vetoed(elements: list, idx: int | None) -> bool:
    """True when the Choice pick is a bare input click (would be vetoed)."""
    if idx is None:
        return False
    el = next((e for e in elements if e.idx == idx), None)
    return el is not None and el.kind in BARE_CLICK_VETO_KINDS


def _apply_proposal(decision, proposed: ProposedAction):
    """Rewrite the decision to the approved/fallback proposal.

    Confidence is boosted past the gate; guard/verify rails still apply
    downstream, so a bad proposal costs ~one step, not the run.
    """
    return replace(
        decision,
        kind=Kind(proposed.kind),
        element_idx=proposed.item
        if proposed.kind in ("click_item", "type_at") else None,
        target_url=proposed.url if proposed.kind == "goto" else None,
        propose_url=proposed.kind == "goto" and proposed.url is None,
        confidence=max(decision.confidence, decision.approval),
    )


def fresh_tabs(known: set[int], current: set[int]) -> set[int]:
    """Tabs the loop hasn't adopted yet (SEE-time reconciliation).

    click_item adopts 0.7s after its click, but slow popups register later.
    Any tab unknown at SEE time gets adopted so perception never strands on
    a stale tab while a fresh result sits unopened beside it.
    """
    return current - known


def no_effect_trip(prev_acted: bool, prev_fp: tuple | None, fp: tuple,
                   settled: bool, last_effect_kind: str | None) -> bool:
    """True when the previous step acted yet the page is observably identical.

    Strictly consecutive only: idle/wait/gate steps neither advance nor
    reset the count — they are patience, and only back-to-back dead actions
    end the run. (Loop-guard separately covers repeated identical intents.)
    """
    return bool(prev_acted and prev_fp is not None and fp == prev_fp
                and settled and last_effect_kind in EFFECT_KINDS)


def notes_url_count(notes: list[str]) -> int:
    """Distinct page URLs banked in the notes buffer (notes store norms)."""
    urls = set()
    for line in notes or []:
        url = line.split(" :: ", 1)[0].strip()
        if url:
            urls.add(url)
    return len(urls)


def should_submit_instead(idx: int, elements: list,
                          last_typed: tuple | None, url: str) -> bool:
    """True when type_at targets the already-filled field it just typed.

    Retyping replaces identical text (clear-before-type) — nothing changes.
    Pressing Enter submits the standing query instead. Scoped to the same
    element on the same URL so multi-field forms are unaffected.
    """
    if last_typed is None or (idx, url) != last_typed:
        return False
    el = next((e for e in elements if e.idx == idx), None)
    return el is not None and el.value_len > 0


# Steps a newly adopted result tab is protected from goto: the loop opened
# this page to READ it, so leaving immediately (the observed goto-back to
# Google) abandons the whole point. Rejections are neutral corrections,
# not no-ops — the other stop rails still terminate honestly.
READING_COOLDOWN_STEPS = 4


def reading_cooldown_active(steps: int, until_step: int) -> bool:
    """True while the loop must stay on its adopted page and read."""
    return steps <= until_step


class Phase(str, Enum):
    """Run-loop phases — the explicit Python state machine.

    Every step walks SEE -> DECIDE -> GATE -> [ACT] -> VERIFY and ends in
    DONE/STOPPED. Idle paths skip ACT (GATE -> VERIFY). DONE is accepted in
    GATE; stops are decided in VERIFY (or STEER). phase_step raises on an
    illegal move so a broken loop fails loudly instead of drifting.
    """

    SEE = "see"
    DECIDE = "decide"
    GATE = "gate"
    ACT = "act"
    VERIFY = "verify"
    DONE = "done"
    STOPPED = "stopped"


_PHASE_EDGES: dict[str, set[str]] = {
    Phase.SEE.value: {Phase.DECIDE.value, Phase.STOPPED.value},
    Phase.DECIDE.value: {Phase.GATE.value},
    Phase.GATE.value: {Phase.ACT.value, Phase.VERIFY.value, Phase.DONE.value, Phase.STOPPED.value},
    Phase.ACT.value: {Phase.VERIFY.value},
    Phase.VERIFY.value: {Phase.SEE.value, Phase.DONE.value, Phase.STOPPED.value},
    Phase.DONE.value: set(),
    Phase.STOPPED.value: set(),
}


def phase_step(phases: list[str], nxt: Phase | str) -> list[str]:
    """Append a phase transition; raise on an illegal move."""
    nxt_v = nxt.value if isinstance(nxt, Phase) else str(nxt)
    if not phases:
        if nxt_v != Phase.SEE.value:
            raise ValueError(f"run must start at SEE, got {nxt_v}")
    elif nxt_v not in _PHASE_EDGES.get(phases[-1], set()):
        raise ValueError(f"illegal phase transition {phases[-1]} -> {nxt_v}")
    phases.append(nxt_v)
    return phases


def credential_placeholder(task: str, *, want_password: bool) -> str | None:
    """Pick a {ENV} placeholder from the task for a credential field."""
    names = re.findall(r"\{([A-Z_][A-Z0-9_]*)\}", task or "")
    if not names:
        return None
    if want_password:
        for n in names:
            if any(k in n for k in ("PASSWORD", "PASSWD", "PASS", "PWD")):
                return "{" + n + "}"
        return "{" + names[-1] + "}"
    for n in names:
        if any(k in n for k in ("USER", "EMAIL", "LOGIN", "NAME")):
            return "{" + n + "}"
    return "{" + names[0] + "}"


async def run_decide_session(
    *,
    start_url: str,
    task: str = "",
    humanize: bool | float = HUMANIZE_LEVEL,
    fps: float = 3.0,
    min_confidence: float = 0.4,
    budget_s: float = 120.0,
    max_steps: int = 50,
    headless: bool = False,
    steer_file: str = "steer.txt",
) -> dict:
    from camoufox.async_api import AsyncCamoufox

    from .browser import CamoufoxPlatform
    from .run.agent_runner import read_steers
    from .run.env_loader import load_env
    from . import report as report_mod
    from . import _log_sink
    from .run.logging_utils import log

    load_env()
    fps = max(2.0, min(5.0, fps))
    interval = 1.0 / fps
    task_text = task.strip() or f"Explore {start_url} and report what you see."
    steer_abs = os.path.abspath(steer_file)
    run_dir = report_mod.make_run_dir()
    _log_sink.set_file(os.path.join(run_dir, "run.log"))
    # Shared cross-run lessons (notebook model): bounded excerpt, loaded once.
    lessons = report_mod.load_lessons(os.path.dirname(os.path.dirname(run_dir)))

    log("=" * 60)
    log("open-typesafe-camoufox (decide) — WATCH THE BROWSER WINDOW (headed) · hands off mouse/keyboard")
    log(f"URL    {start_url}")
    log(f"TASK   {task_text}")
    log(f"MODE   headed={not headless} humanize={humanize} min_conf={min_confidence}")
    log(f"BUDGET {budget_s:.0f}s / {max_steps} steps")
    log(f"STEER  {steer_abs}  (lines: 'stop' | 'goto <url>' | 'pause' | 'resume' | any instruction)")
    log(f"LOG    {run_dir}")
    log("=" * 60)

    platform = CamoufoxPlatform()
    state = RunState(task=task_text, url=start_url, log=[task_text])
    history: list[str] = []
    instructions: list[str] = []
    steps = 0
    moves = 0
    done = False
    stopped = False
    stop_reason = ""
    paused = False
    noops = 0
    last_sig: tuple | None = None
    sig_run = 0
    last_typed: tuple | None = None  # (element idx, page url) of the last successful type
    # Long-horizon tracking (40-50 steps): visited-URL memory, extractive
    # notes that survive the 8-line history window, and dead-run detection
    # for actions with no observable effect.
    visited: list[str] = []
    visited_set: set[str] = set()
    notes: list[str] = []
    prev_fp: tuple | None = None
    dead_run = 0
    prev_acted = False
    synth_attempted = False
    last_effect_kind: str | None = None
    known_tab_ids: set[int] = set()
    reading_until_step: int = 0
    last_page_id: int | None = None
    phase_trail: list[str] = []
    steer_consumed = 0
    started = datetime.now().isoformat(timespec="seconds")
    t_end = time.time() + budget_s

    async with AsyncCamoufox(headless=headless, humanize=humanize) as browser:
        page = await browser.new_page()
        await platform.set_page(page)
        try:
            await asyncio.wait_for(platform.safe_goto(start_url), timeout=60.0)
        except Exception as exc:
            log(f"initial goto failed ({exc}), retrying plain goto")
            await page.goto(start_url, wait_until="load")
            platform._last_url = page.url
            await platform.reinject_tracker()
        await platform.start_cursor_tracking()
        log(f"tracker selftest: {'PASS' if await platform.cursor_selftest() else 'WARN — cursor.json may be incomplete'}")
        known_tab_ids = platform.tab_ids()
        last_page_id = id(platform.page)

        while time.time() < t_end and steps < max_steps and not done and not stopped:
            if paused:
                log("PAUSED — human has the cursor; steer 'resume' to continue")
                await asyncio.sleep(5)
                continue
            steps += 1
            entry: dict = {"n": steps, "t": round(time.time(), 1)}
            log(f"──── step {steps}/{max_steps} " + "─" * 40)
            phase_step(phase_trail, Phase.SEE)
            effective_task = task_text + (
                "\nNEW INSTRUCTIONS:\n" + "\n".join(instructions[-5:])
                if instructions else ""
            )

            # SEE
            n_tabs = await platform.tab_count()
            # SEE-time reconciliation: click_item adopts 0.7s after its
            # click, but slow popups register later. Adopt anything unknown
            # now so perception reads the fresh result, not a stale tab.
            current_ids = platform.tab_ids()
            if known_tab_ids and fresh_tabs(known_tab_ids, current_ids):
                fresh_url = await platform.adopt_new_tab(known_tab_ids)
                if fresh_url:
                    msg = f"step {steps}: adopted slow popup — {fresh_url}"
                    log(f"TABS   {msg}")
                    history.append(msg)
                    entry.setdefault("tabs", []).append(f"adopted {fresh_url}")
            known_tab_ids = platform.tab_ids()
            page = platform.page
            try:
                raw_png = await page.screenshot(type="png")
            except Exception as exc:
                log(f"SEE    screenshot failed: {exc}")
                entry["see"] = f"screenshot failed: {exc}"
                history.append(f"step {steps}: screenshot failed")
                report_mod.append_transcript(run_dir, entry)
                await asyncio.sleep(interval)
                continue
            elements = await perception.find_elements(platform)
            focused = await perception.get_focused_field(platform)
            page_text = await perception.get_page_text(platform)
            n_tabs = await platform.tab_count()
            # TAB SWITCH: a new platform page means an adoption happened —
            # start the reading cooldown and recapture the DOM digest now so
            # the fresh tab is read in context instead of abandoned.
            if last_page_id is not None and id(platform.page) != last_page_id:
                reading_until_step = steps + READING_COOLDOWN_STEPS
                head = page_text.strip().replace("\n", " ")[:120]
                msg = (f"step {steps}: TAB SWITCH — reading {platform.page.url} "
                       f"({len(elements)} elements, {len(page_text.strip())}ch"
                       f"{' :: ' + head if head else ''}); "
                       f"goto refused until step {reading_until_step}")
                log(f"TABS   {msg}")
                history.append(msg)
                entry.setdefault("tabs", []).append(f"switched; cooldown to {reading_until_step}")
            last_page_id = id(platform.page)
            try:
                page_host = urllib.parse.urlparse(page.url).netloc.lower()
            except ValueError:
                page_host = ""
            n_links = sum(1 for e in elements if e.kind == "link")
            n_ext = sum(1 for e in elements
                        if e.kind == "link" and (h := perception.host_of(e.href)) and h != page_host)
            # READ + remember: first settled sighting of a page banks an
            # extractive note (survives the history window across 40-50 steps).
            norm = perception.norm_url(page.url)
            if page_settled(page_text) and norm not in visited_set:
                visited.append(norm)
                visited_set.add(norm)
                banked = f"{norm} :: {perception.excerpt_for(task_text, page_text)}"
                notes.append(banked)
                notes[:] = perception.trim_notes(notes)
                report_mod.append_memory(run_dir, f"- {banked}")
            # Blocked (bot-check/captcha) pages stay in the loop: flag the
            # state and keep classifying positions + moving the cursor.
            blocked = perception.is_blocked_page(page.url, page_text)
            if blocked:
                msg = (f"step {steps}: page flagged {blocked} — working it "
                       "like any page (classify, move, select, repeat)")
                log(f"BLOCKED {msg}")
                history.append(msg)
                entry["blocked"] = blocked
            state.url = page.url
            state.elements = elements
            state.focused = focused
            state.page_text = page_text
            state.history = list(history)
            report_mod.write_raw_png(run_dir, steps, raw_png)
            see_line = (
                f"{len(elements)} elements ({n_links} links, {n_ext} external) · tabs={n_tabs} · focused={focused.role or '-'}"
                f"{' (credential)' if focused.is_credential else ''} · "
                f"text={len(page_text.strip())}ch"
            )
            log(f"SEE    {see_line}")
            entry["see"] = see_line
            if page_text.strip():
                log(f"TEXT   {page_text.strip()[:160]}")
            entry["page_text"] = page_text[:400]

            # STEER
            new_steers, steer_consumed = read_steers(steer_abs, steer_consumed)
            for kind_s, payload in new_steers:
                label = payload if kind_s == "goto" else (payload or "stop")
                log(f"STEER  {label}")
                entry.setdefault("steer", []).append(label)
                if kind_s == "stop":
                    stopped = True
                    stop_reason = "steer stop"
                    break
                if kind_s == "pause":
                    paused = True
                elif kind_s == "resume":
                    paused = False
                if kind_s == "goto":
                    res = await goto_url(platform, payload or "")
                    log(f"RESULT {res}")
                    history.append(f"step {steps}: steer goto — {res}")
                    moves += 1
                elif kind_s == "instruction" and payload:
                    instructions.append(payload)
            if stopped or paused:
                if stopped:
                    phase_step(phase_trail, Phase.STOPPED)
                entry["phases"] = list(phase_trail)
                report_mod.append_transcript(run_dir, entry)
                if stopped:
                    break
                await asyncio.sleep(interval)
                continue

            # PROPOSE: the small model reads every element + URL and poses
            # the single best next action as a yes/no question. Jev answers
            # it as the approval Noul below — LLM reasons, Noul confirms.
            # The question is wrapped with the candidate + rationale as
            # structured supporting data (docs: structure sharpens Nouls).
            # On a freshly adopted result page the model is told to read it,
            # not navigate away (matches the goto reading cooldown).
            proposed = None
            approval_struct: dict | str | None = None
            reading_note = ""
            if reading_cooldown_active(steps, reading_until_step):
                reading_note = (
                    f"CURRENTLY READING (adopted result page): {page.url} — "
                    "read THIS page's content toward the task; do NOT propose "
                    "goto or leaving this page."
                )
            try:
                proposed = await propose_action(
                    task=effective_task, url=page.url, elements=elements,
                    page_text=page_text, history=history, notes=notes,
                    reading_note=reading_note,
                )
            except Exception as exc:  # noqa: BLE001
                log(f"PROPOSE failed ({exc.__class__.__name__})")
            if proposed is not None:
                log(f"PROPOSE {proposed.kind}"
                    + (f" #{proposed.item}" if proposed.item is not None else "")
                    + (f" {proposed.url}" if proposed.url else "")
                    + f" : {proposed.rationale[:100]}")
                entry["propose"] = (
                    f"{proposed.kind} #{proposed.item} : {proposed.rationale[:100]}"
                )
                approval_struct = {
                    "question": proposed.question,
                    "candidate": {
                        "kind": proposed.kind,
                        "item": proposed.item,
                        "url": proposed.url,
                    },
                    "rationale": proposed.rationale,
                    "focus": "Answer YES only if this exact action is the best "
                             "next step toward the TASK; answer NO if any other "
                             "action (including waiting) serves the task better.",
                }

            # DECIDE
            try:
                decision = await decide_action(
                    task=effective_task, url=page.url, start_url=start_url,
                    elements=elements, focused=focused, page_text=page_text,
                    history=history, tabs=n_tabs, notes=notes, visited=visited,
                    lessons=lessons, blocked=blocked,
                    approval_question=approval_struct,
                )
            except Exception as exc:
                log(f"DECIDE failed ({exc.__class__.__name__}); idling this step")
                entry["decide"] = f"failed: {exc}"
                history.append(f"step {steps}: decide failed, idled")
                noops += 1
                phase_step(phase_trail, Phase.DECIDE)
                phase_step(phase_trail, Phase.GATE)
                phase_step(phase_trail, Phase.VERIFY)
                entry["phases"] = list(phase_trail)
                report_mod.append_transcript(run_dir, entry)
                await asyncio.sleep(interval)
                continue
            conf = decision.confidence
            phase_step(phase_trail, Phase.DECIDE)
            # The model-side settle flag can only add patience, never remove
            # the regex guard: a loading verdict forces wait even on readable
            # text, but a ready verdict never overrides a loading regex.
            ready_now = page_settled(page_text) and decision.page_ready >= 0.35
            log(f"DECIDE {decision.kind.value} conf={conf:.2f}"
                + (f" item=#{decision.element_idx}" if decision.element_idx is not None else "")
                + (f" site={decision.target_url or 'other...'}" if decision.kind == Kind.GOTO else "")
                + f" | ready={decision.page_ready:.2f} text?={decision.needs_text:.2f}"
                + f" done?={decision.task_done:.2f} prog={decision.progress:.2f}"
                + f" appr={decision.approval:.2f} fit={decision.fits:.2f}")
            entry["decide"] = (
                f"{decision.kind.value} conf={conf:.2f} "
                f"item={decision.element_idx} site={decision.target_url or ('other' if decision.propose_url else None)}"
            )
            entry["nouls"] = {
                "ready": round(decision.page_ready, 2),
                "text?": round(decision.needs_text, 2),
                "done?": round(decision.task_done, 2),
                "approve": round(decision.approval, 2),
                "fit": round(decision.fits, 2),
            }
            entry["progress"] = round(decision.progress, 2)
            entry["think"] = entry["decide"]
            # Approval override: the LLM reasoned over all elements + URLs
            # and the Noul leans yes on a DIFFERENT target than the Choice
            # vote — execute the proposal. Agreement runs the normal path.
            # A vetoed/mismatched Choice pick falls back to a viable proposal,
            # or idles neutrally when none exists (never execute known-dead).
            route = resolve_proposal_action(
                choice_kind=decision.kind.value, choice_item=decision.element_idx,
                elements=elements, fits=decision.fits, proposed=proposed,
                executable=proposal_executable(proposed, elements),
                approval=decision.approval)
            if route == "override":
                log(f"APPROVED {proposed.kind}"
                    + (f" #{proposed.item}" if proposed.item is not None else "")
                    + f" (approval={decision.approval:.2f}) — executing proposal over Choice")
                history.append(f"step {steps}: approved proposal — {proposed.rationale[:100]}")
                entry["decide"] += " [approved-override]"
                decision = _apply_proposal(decision, proposed)
                conf = decision.confidence
            elif route == "fallback":
                log(f"VETO-FALLBACK {proposed.kind}"
                    + (f" #{proposed.item}" if proposed.item is not None else "")
                    + f" (approval={decision.approval:.2f}) — Choice pick vetoed/mismatched, trying proposal")
                history.append(f"step {steps}: veto fallback — {proposed.rationale[:100]}")
                entry["decide"] += " [veto-fallback]"
                decision = _apply_proposal(decision, proposed)
                conf = decision.confidence
            elif route == "mismatch-idle":
                log(f"MISMATCH {decision.kind.value} #{decision.element_idx} "
                    f"(fit={decision.fits:.2f}) — waiting neutrally")
                history.append(f"step {steps}: pick mismatched its verb (fit={decision.fits:.2f}), waited")
                entry["act"] = f"mismatch wait ({decision.kind.value} #{decision.element_idx})"
                decision = replace(decision, kind=Kind.WAIT, element_idx=None)
            # Retype intent on an already-filled field means submit: the query
            # is in the box, typing it again changes nothing — Enter does.
            if (decision.kind == Kind.TYPE_AT and decision.element_idx is not None
                    and should_submit_instead(decision.element_idx, elements,
                                              last_typed, page.url)):
                log(f"RETYPE type_at #{decision.element_idx} on filled field — submitting instead")
                history.append(f"step {steps}: retype on filled field, submitting instead")
                entry["decide"] += " [retype-submit]"
                decision = replace(decision, kind=Kind.PRESS_ENTER, element_idx=None)
                last_typed = None
            report_mod.write_answers_json(run_dir, steps, decision.raw)

            # Loop-guard: the tab, clear, and click systems all report into
            # the decision, but a confident classifier can still fixate
            # (same click 5x). Three identical click/type targets in a row
            # force a wait here instead of executing again.
            guard_trip = False
            if decision.kind in (Kind.CLICK_ITEM, Kind.TYPE_AT) and decision.element_idx is not None:
                guard_sig = (
                    decision.kind.value, decision.element_idx,
                    next(((e.label or e.text or "")[:40] for e in elements
                          if e.idx == decision.element_idx), ""),
                    next((perception.host_of(e.href) for e in elements
                          if e.idx == decision.element_idx), ""),
                    page.url,
                )
                guard_trip, sig_run = loop_guard_trip(last_sig, sig_run, guard_sig)
                last_sig = guard_sig
            phase_step(phase_trail, Phase.GATE)
            acted = False
            if conf < min_confidence and decision.kind not in (Kind.DONE,):
                log(f"GATE   conf {conf:.2f} < {min_confidence} — idle (no-op {noops + 1})")
                history.append(f"step {steps}: low conf {conf:.2f}, idled")
                entry["act"] = "idle (low confidence)"
                noops += 1
            elif guard_trip:
                log(f"LOOPGUARD {decision.kind.value} #{decision.element_idx} x{sig_run} — forced wait (no-op {noops + 1})")
                history.append(f"step {steps}: loopguard tripped on {decision.kind.value} #{decision.element_idx}, waited")
                entry["act"] = f"loopguard wait ({decision.kind.value} #{decision.element_idx})"
                noops += 1
            elif decision.kind == Kind.WAIT:
                # Patience, not doubt: the screen is still loading. Neutral —
                # it neither resets nor advances the no-op count, so a slow
                # page can't trigger the two-no-op stop by itself.
                log("IDLE   wait (page loading — neutral)")
                history.append(f"step {steps}: wait")
                entry["act"] = "wait"
            elif decision.kind == Kind.NONE:
                log(f"IDLE   none (no-op {noops + 1})")
                history.append(f"step {steps}: none")
                entry["act"] = "none"
                noops += 1
            elif decision.kind == Kind.DONE:
                if not ready_now:
                    msg = (f"step {steps}: DONE REJECTED (page not settled — "
                           f"regex={page_settled(page_text)}, model ready={decision.page_ready:.2f}) — "
                           "wait one step and re-observe")
                    log(f"DONE denied: {msg}")
                    history.append(msg)
                    entry["act"] = "DONE rejected (page not settled)"
                elif decision.task_done < 0.5:
                    msg = (f"step {steps}: DONE REJECTED (model completion flag "
                           f"{decision.task_done:.2f} < 0.5) — keep working")
                    log(f"DONE denied: {msg}")
                    history.append(msg)
                    entry["act"] = "DONE rejected (model flag)"
                elif not page_text.strip():
                    msg = f"step {steps}: DONE REJECTED (empty page text)"
                    log(f"DONE denied: {msg}")
                    history.append(msg)
                    entry["act"] = "DONE rejected (empty page)"
                else:
                    full_note = " | ".join(notes[-3:]) or page_text.strip()
                    note = full_note[:300]
                    done = True
                    phase_step(phase_trail, Phase.DONE)
                    entry["act"] = f"DONE ({note[:80]})"
                    history.append(f"step {steps}: DONE — {note[:80]}")
                    log(f"DONE   {note[:120]}")
            elif decision.kind == Kind.GOTO:
                if reading_cooldown_active(steps, reading_until_step):
                    msg = (f"step {steps}: GOTO REJECTED (reading cooldown to step "
                           f"{reading_until_step}) — read the adopted result page first")
                    log(f"GOTO denied: {msg}")
                    history.append(msg)
                    entry["act"] = "goto rejected (reading)"
                else:
                    target = decision.target_url
                    if decision.propose_url or not target:
                        proposed = await propose_url(task=effective_task, history=history)
                        target = proposed.url if proposed.ok else None
                    if not target:
                        log("GOTO   no valid URL — idle")
                        history.append(f"step {steps}: goto without URL, idled")
                        entry["act"] = "goto (no URL)"
                        noops += 1
                    else:
                        log(f"ACT    goto {target}")
                        res = await goto_url(platform, target)
                        log(f"RESULT {res}")
                        if action_failed(res):
                            history.append(f"step {steps}: goto failed — {res}")
                            entry["act"] = f"goto {target}"
                            entry["result"] = res
                            noops += 1
                        else:
                            entry["act"] = f"goto {target}"
                            entry["result"] = res
                            history.append(f"step {steps}: goto {target} — {res}")
                            moves += 1
                            noops = 0
                            acted = True
            elif decision.kind == Kind.PRESS_ENTER:
                log("ACT    key=Enter")
                res = await press_key(platform, "Enter")
                log(f"RESULT {res}")
                if action_failed(res):
                    history.append(f"step {steps}: Enter failed — {res}")
                    entry["act"] = "key=Enter"
                    entry["result"] = res
                    noops += 1
                else:
                    entry["act"] = "key=Enter"
                    entry["result"] = res
                    history.append(f"step {steps}: pressed Enter — {res}")
                    moves += 1
                    noops = 0
                    acted = True
                    last_effect_kind = "press_enter"
            elif decision.kind == Kind.PRESS_ESCAPE:
                log("ACT    key=Escape")
                res = await press_key(platform, "Escape")
                log(f"RESULT {res}")
                if action_failed(res):
                    history.append(f"step {steps}: Escape failed — {res}")
                    entry["act"] = "key=Escape"
                    entry["result"] = res
                    noops += 1
                else:
                    entry["act"] = "key=Escape"
                    entry["result"] = res
                    history.append(f"step {steps}: pressed Escape — {res}")
                    moves += 1
                    noops = 0
                    acted = True
                    last_effect_kind = "press_escape"
            elif decision.kind == Kind.REFRESH:
                log("ACT    refresh")
                res = await platform.refresh_page()
                log(f"RESULT {res}")
                if action_failed(res):
                    history.append(f"step {steps}: refresh failed — {res}")
                    entry["act"] = "refresh"
                    entry["result"] = res
                    noops += 1
                else:
                    entry["act"] = "refresh"
                    entry["result"] = res
                    history.append(f"step {steps}: refreshed — {res}")
                    moves += 1
                    noops = 0
                    acted = True
                    last_effect_kind = "refresh"
            elif decision.kind == Kind.BACK:
                log("ACT    back")
                res = await platform.go_back()
                log(f"RESULT {res}")
                if action_failed(res):
                    history.append(f"step {steps}: back failed — {res}")
                    entry["act"] = "back"
                    entry["result"] = res
                    noops += 1
                else:
                    entry["act"] = "back"
                    entry["result"] = res
                    history.append(f"step {steps}: went back — {res}")
                    moves += 1
                    noops = 0
                    acted = True
                    last_effect_kind = "back"
            elif decision.kind == Kind.CLOSE_OTHERS:
                log("ACT    close other tabs")
                n_closed = await platform.close_other_tabs()
                res = f"closed {n_closed} other tab(s)"
                log(f"RESULT {res}")
                entry["act"] = "close_others"
                entry["result"] = res
                history.append(f"step {steps}: {res}")
                moves += 1
                noops = 0
                acted = True
            else:  # CLICK_ITEM / TYPE_AT
                idx = decision.element_idx
                by_idx = {e.idx: e for e in elements}
                if idx is None or idx not in by_idx:
                    log(f"ACT    {decision.kind.value} without valid item — idle")
                    history.append(f"step {steps}: {decision.kind.value} without item, idled")
                    entry["act"] = f"{decision.kind.value} (no item)"
                    noops += 1
                elif decision.kind == Kind.CLICK_ITEM:
                    target = by_idx[idx]
                    if target.kind in BARE_CLICK_VETO_KINDS:
                        msg = (f"step {steps}: vetoed bare click on {target.kind} #{idx} — "
                               "text fields are typed (type_at), never bare-clicked")
                        log(f"VETO   {msg}")
                        history.append(msg)
                        entry["act"] = f"click vetoed ({target.kind} #{idx})"
                        noops += 1
                    else:
                        log(f"ACT    element #{idx} (scroll+circle+click)")
                        res = await click_item(platform, elements, idx,
                                                     expected_kind=by_idx[idx].kind)
                        log(f"RESULT {res}")
                        if action_failed(res):
                            history.append(f"step {steps}: click failed — {res}")
                            entry["act"] = f"element #{idx} click"
                            entry["result"] = res
                            noops += 1
                        else:
                            entry["act"] = f"element #{idx} click"
                            entry["result"] = res
                            history.append(f"step {steps}: clicked element #{idx} — {res}")
                            moves += 1
                            noops = 0
                            acted = True
                            last_effect_kind = "click_item"
                else:  # TYPE_AT
                    elem = by_idx[idx]
                    label = elem.label or elem.placeholder or elem.text or elem.id or f"element #{idx}"
                    cred = is_credential_element(elem)
                    log(f"ACT    element #{idx} type (credential={cred})")
                    if not cred and decision.needs_text < 0.35:
                        msg = (f"step {steps}: model says no text needed at #{idx} "
                               f"(needs_text={decision.needs_text:.2f}) — skipping compose")
                        log(msg)
                        history.append(msg)
                        entry["act"] = f"type_at #{idx} (no text needed)"
                        noops += 1
                        composed = None  # type: ignore[assignment]
                    else:
                        composed = await compose_text(
                            task=effective_task, field_label=label,
                            placeholder=elem.placeholder, nearby_text=page_text,
                            history=history, is_credential=cred,
                        )
                    text_to_type: str | None = None
                    if composed is not None and composed.fill and composed.text.strip():
                        text_to_type = composed.text.strip()
                    elif cred:
                        ph = credential_placeholder(effective_task, want_password=True)
                        if ph and os.environ.get(ph.strip("{}")):
                            text_to_type = ph
                        else:
                            msg = (f"step {steps}: credential field #{idx} but no usable "
                                   "{ENV} placeholder — skipping")
                            log(msg)
                            history.append(msg)
                            entry["act"] = f"type_at #{idx} (credential, no placeholder)"
                            noops += 1
                    else:
                        if composed is not None:
                            history.append(f"step {steps}: writer declined to fill #{idx}")
                            entry["act"] = f"type_at #{idx} (writer declined)"
                            noops += 1
                        # composed None = model said no text needed (recorded above).
                    if text_to_type is not None:
                        res = await type_at(platform, elements, idx, text_to_type,
                                                  expected_kind=elem.kind)
                        log(f"RESULT {res}")
                        if action_failed(res):
                            history.append(f"step {steps}: type failed — {res}")
                            entry["act"] = f"element #{idx} type={len(text_to_type)}ch"
                            entry["result"] = res
                            noops += 1
                        else:
                            entry["act"] = f"element #{idx} type={len(text_to_type)}ch"
                            entry["result"] = res
                            history.append(f"step {steps}: typed at #{idx} — {res}")
                            moves += 1
                            noops = 0
                            acted = True
                            last_effect_kind = "type_at"
                            last_typed = (idx, page.url)

            # The action may have rebound the platform to a new tab
            # (click auto-adopt) — re-sync the local handle so the screenshot,
            # url, and artifacts below all read the CURRENT tab.
            page = platform.page

            # REPORT artifacts for this step
            from .decide import build_questions
            from .perception import build_state as _build_state

            state_packet = _build_state(
                task=effective_task, url=page.url, elements=elements,
                focused=focused, page_text=page_text, history=history,
                tabs=n_tabs, notes=notes, visited=visited, lessons=lessons,
                blocked=blocked,
            )
            sites = [u for u in (start_url, page.url) if u]
            sites = list(dict.fromkeys(sites))
            report_mod.write_annotated_png(
                run_dir, steps, raw_png, elements,
                chosen_idx=decision.element_idx, focused_frame=focused.frame or None,
            )
            report_mod.write_payload_txt(
                run_dir, steps, state_packet, build_questions(elements, sites, visited),
                f"{decision.kind.value} conf={decision.confidence:.2f} "
                f"item={decision.element_idx} "
                f"ready={decision.page_ready:.2f} text?={decision.needs_text:.2f} "
                f"done?={decision.task_done:.2f} prog={decision.progress:.2f} "
                f"appr={decision.approval:.2f}",
            )
            entry["phases"] = list(phase_trail)
            report_mod.write_wire(run_dir, {
                "n": steps, "t": entry.get("t"), "url": page.url, "tabs": n_tabs,
                "see": entry.get("see"), "decide": entry.get("decide"),
                "nouls": entry.get("nouls"), "progress": entry.get("progress"),
                "act": entry.get("act"), "result": entry.get("result"),
                "phases": entry.get("phases"),
                "focused": {
                    "role": focused.role, "label": focused.label,
                    "placeholder": focused.placeholder,
                    "value_len": len(focused.value),
                    "is_credential": focused.is_credential,
                },
                "page_text": page_text,
                "elements": [
                    {"idx": e.idx, "kind": e.kind, "type": e.type,
                     "label": e.label, "placeholder": e.placeholder,
                     "text": e.text, "value_len": e.value_len,
                     "href": e.href, "region": e.region,
                     "host": perception.host_of(e.href),
                     "sel": f'[data-jev="{e.idx}"]',
                     "cx": e.cx, "cy": e.cy}
                    for e in elements
                ],
                "notes": notes, "visited": visited,
            })
            report_mod.append_transcript(run_dir, entry)

            # VERIFY: fingerprint the step's outcome and evaluate the stop
            # rules. A settled page identical to the previous step means the
            # last action changed nothing readable. Twice in a row ends the
            # run with an honest reason instead of looping to max_steps.
            if not done:
                if acted:
                    phase_step(phase_trail, Phase.ACT)
                phase_step(phase_trail, Phase.VERIFY)
            # VERIFY: fingerprint the step's outcome and evaluate the stop
            # rules. Only a step that ACTED can prove the previous action
            # dead: idle/wait/gate steps are patience, not evidence.
            fp = perception.page_fingerprint(page.url, page_text)
            if no_effect_trip(prev_acted, prev_fp, fp, page_settled(page_text),
                              last_effect_kind):
                dead_run += 1
                history.append(f"step {steps}: no observable effect from {last_effect_kind} x{dead_run}")
                log(f"NOEFFECT {last_effect_kind} changed nothing x{dead_run}")
                if dead_run >= 2:
                    stopped = True
                    stop_reason = "action had no observable effect twice"
                    phase_step(phase_trail, Phase.STOPPED)
            else:
                dead_run = 0
            prev_fp = fp
            prev_acted = acted

            # Last-resort synthesis: the per-step Noul never flags completion,
            # but the chat model can judge across pages. Once per run, when a
            # stop is imminent and 2+ distinct pages were read, ask it to call
            # the task from the notes. A refusal (or failure) falls through
            # to the honest stop below.
            if (not done and not stopped and not synth_attempted
                    and (dead_run >= 2 or noops >= STOP_AFTER_NOOPS)
                    and notes_url_count(notes) >= 2):
                synth_attempted = True
                log("SYNTH  judging completion from notes...")
                try:
                    verdict = await summarize_task(task=effective_task, notes=notes)
                except Exception as exc:  # noqa: BLE001
                    verdict = None
                    log(f"SYNTH  failed ({exc.__class__.__name__})")
                if verdict is not None and verdict.done:
                    done = True
                    phase_step(phase_trail, Phase.DONE)
                    entry["act"] = f"DONE ({verdict.note[:80]})"
                    history.append(f"step {steps}: DONE (synthesized) — {verdict.note[:80]}")
                    log(f"DONE   {verdict.note[:120]}")

            if noops >= STOP_AFTER_NOOPS and not done and not stopped:
                stopped = True
                stop_reason = f"{STOP_AFTER_NOOPS} consecutive no-ops"
                phase_step(phase_trail, Phase.STOPPED)
                log(f"STOP   {STOP_AFTER_NOOPS} consecutive no-ops — ending run")
            await asyncio.sleep(interval)

        cursor = await platform.harvest_cursor_events()
        cursor_path = report_mod.write_cursor(run_dir, cursor)
        summary = {
            "url": start_url, "task": task_text, "started": started,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "steps": steps, "moves": moves, "task_done": done,
            "stopped": stopped, "stop_reason": stop_reason,
            "cursor_moves": len(cursor["moves"]),
            "cursor_clicks": len(cursor["clicks"]),
            "run_dir": run_dir,
        }
        report_mod.write_run_json(run_dir, summary)
        outcome = (f"OUTCOME done={done} stopped={stopped} "
                   f"({summary.get('stop_reason', '')}) steps={steps} moves={moves}")
        report_mod.append_memory(run_dir, outcome)
        report_mod.append_transcript(run_dir, {"final": summary})

    log("=" * 60)
    log(f"SUMMARY steps={steps} moves={moves} done={done} stopped={stopped} {stop_reason}")
    log(f"CURSOR {len(cursor['moves'])} moves + {len(cursor['clicks'])} clicks -> {cursor_path}")
    log(f"TRANSCRIPT {os.path.join(run_dir, 'transcript.jsonl')}")
    log("=" * 60)
    return {"state": state, "steps": steps, "moves": moves, "done": done,
            "stopped": stopped, "stop_reason": stop_reason,
            "cursor": cursor, "run_dir": run_dir}
