"""
runner.loop — the main decide session loop (split from runner.py).

Contains ``run_decide_session`` and the step-level sub-functions that
compose one iteration of the Jev-primary step loop:

  _step_see            SEE phase (screenshot, perception, banked notes)
  _step_steer          STEER phase (steer-file commands)
  _step_propose_decide PROPOSE + DECIDE (writer proposal, Jev decision)
  _step_gate_act       GATE + ACT (gates, branching handlers, heal)
  _step_verify_recover VERIFY + RECOVER (fingerprint, recovery, stops)
  _write_step_record   wire record + transcript + step JSONL
  _post_loop_summary   post-loop summary and artifacts
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import urllib.parse
from dataclasses import replace
from datetime import datetime

from .. import perception
from ..actions import (
    action_failed,
    challenge_control,
    click_item,
    execute_recovery,
    goto_url,
    press_key,
    type_at,
)
from ..capability.dynamic_registry import register_capability
from ..capability.resolve import heal_target, read_input_value
from ..capability.validator import validate_capability
from ..decide import (
    HealStrategy,
    Kind,
    decide_action,
    decide_captcha_action,
    decide_heal_action,
    decide_recovery_action,
    decide_restart_action,
)
from ..deps import RunState
from ..frontier import Frontier
from ..machine.run_engine import RunMachine, advance, state_id
from ..perception import is_credential_element
from ..writer import compose_text, propose_action, propose_url, summarize_task, synthesize_capability

from .constants import BARE_CLICK_VETO_KINDS, CAPTCHA_MAX_ATTEMPTS, READING_COOLDOWN_STEPS
from .gates import confidence_gated, reading_cooldown_active, select_recovery
from .helpers import (
    captcha_should_stop,
    classify_noop,
    credential_placeholder,
    escalation_target,
    fresh_tabs,
    is_blank_page,
    loop_guard_trip,
    notes_url_count,
    no_effect_trip,
    page_settled,
    restart_candidates,
    screenshot_dead,
    screenshot_should_restart,
    should_submit_instead,
    step_verdict,
    stop_limits,
)
from .proposal import _apply_proposal, proposal_executable, resolve_proposal_action
from .._log_sink import kv as _kv


# ---------------------------------------------------------------------------
# SEE
# ---------------------------------------------------------------------------


async def _step_see(
    *,
    platform,
    entry: dict,
    steps: int,
    shot_streak: int,
    known_tab_ids: set[int],
    last_page_id: int | None,
    reading_until_step: int,
    visited: list[str],
    visited_set: set[str],
    notes: list[str],
    history: list[str],
    task_text: str,
    run_dir: str,
    report_mod,
    interval: float,
    frontier=None,
) -> dict:
    """Execute the SEE phase; return the observed state for the loop.

    On a screenshot failure the failure is recorded and (when warranted) a
    Jev-scored restart is issued; the caller then re-observes on the next
    step. The screenshot-failure paths are neutral: they append the
    transcript entry and sleep one interval.
    """
    from ..run.logging_utils import log

    n_tabs = await platform.tab_count()
    # SEE-time reconciliation: click_item adopts 0.7s after its
    # click, but slow popups register later. Adopt anything unknown
    # now so perception reads the fresh result, not a stale tab.
    current_ids = platform.tab_ids()
    if known_tab_ids and fresh_tabs(known_tab_ids, current_ids):
        fresh_url = await platform.adopt_new_tab(known_tab_ids)
        if fresh_url:
            msg = f"step {steps}: adopted slow popup — {fresh_url}"
            _kv("jev-solver:tabs:adopted", detail=msg[:160])
            history.append(msg)
            entry.setdefault("tabs", []).append(f"adopted {fresh_url}")
    known_tab_ids = platform.tab_ids()
    page = platform.page

    stopped = False
    stop_reason = ""
    restarted = False
    try:
        raw_png = await page.screenshot(type="png")
        shot_streak = 0
    except Exception as exc:
        shot_streak += 1
        _kv("jev-solver:page:see", status="screenshot-failed",
            streak=shot_streak, error=exc.__class__.__name__)
        entry["see"] = f"screenshot failed x{shot_streak}: {exc}"
        history.append(f"step {steps}: screenshot failed x{shot_streak}")
        entry["verdict"] = "unresolved"
        if screenshot_dead(shot_streak):
            stopped = True
            stop_reason = (f"page unresponsive: {shot_streak} consecutive "
                           f"screenshot failures — mission relaunches fresh")
            entry["restart"] = {"attempts": entry.get("restart_attempts", 0),
                                "outcome": "terminal"}
            _kv("jev-solver:run:stop", reason=stop_reason)
            machine = entry.get("_machine")
            phase_trail = entry.get("_phase_trail", [])
            if machine is not None and phase_trail is not None:
                if state_id(machine) == "see":
                    advance(machine, phase_trail, "abort")  # see -> stopped
                else:
                    _kv("jev-solver:run:stop",
                        reason=f"machine at {state_id(machine)}, "
                               "trail ends without terminal")
        elif screenshot_should_restart(shot_streak):
            restarted = True
            entry["restart_attempts"] = entry.get("restart_attempts", 0) + 1
            try:
                current = page.url
            except Exception:
                current = ""
            cands = restart_candidates(visited, current)
            action, url, conf = await decide_restart_action(
                candidates=cands, current_url=current,
                streak=shot_streak)
            if action != "goto" or conf < 0.4 or not url:
                action, url = "back", None
            if action == "back":
                try:
                    res = await platform.go_back()
                except Exception as exc2:
                    res = f"error: back failed: {exc2}"
                dest = "history-back"
            else:
                res = await goto_url(platform, url or "")
                dest = url
            history.append(f"step {steps}: restart -> {dest} (jev-scored {action}) — {str(res)[:100]}")
            entry["restart"] = {"streak": shot_streak,
                                "tries": entry.get("restart_attempts", 0),
                                "action": action, "dest": dest,
                                "result": str(res)[:200]}
            _kv("jev-solver:run:restart", action=action, dest=dest,
                result=str(res)[:100])
        report_mod.append_transcript(run_dir, entry)
        await asyncio.sleep(interval)
        return {"restarted": restarted, "shot_streak": shot_streak,
                "known_tab_ids": known_tab_ids, "stopped": stopped,
                "stop_reason": stop_reason,
                # The failure was fully accounted above (transcript +
                # sleep); the caller must skip the rest of the step.
                # Needed because plain failures (no restart, no stop)
                # return restarted=False yet have no observation to
                # continue with (no raw_png/elements/page_text).
                "skip_step": True}

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
        # Scored recall: Reader JSON → chunks → memory.jsonl → rerank
        # vs task → winners join the notes JEV reasons over. Any
        # failure keeps the excerpt above (fail-soft, offline-safe).
        try:
            from .memory import recall_page as _recall_page

            try:
                _live_html = await asyncio.wait_for(
                    platform.page.content(), timeout=10.0)
            except Exception:  # noqa: BLE001
                _live_html = ""
            recalled = await _recall_page(
                run_dir=run_dir, report_mod=report_mod, url=page.url,
                task_text=task_text, context_note=banked,
                page_html=_live_html or "")
        except Exception:  # noqa: BLE001
            recalled = []
        for _line in recalled or []:
            notes.append(_line)
        if recalled:
            notes[:] = perception.trim_notes(notes)
            _kv("jev-solver:memory:recalled", chunks=len(recalled))
    # Blocked (bot-check/captcha) pages stay in the loop: flag the
    # state and keep classifying positions + moving the cursor.
    blocked = perception.is_blocked_page(page.url, page_text)
    if blocked:
        msg = (f"step {steps}: page flagged {blocked} — working it "
               "like any page (classify, move, select, repeat)")
        _kv("jev-solver:challenge:blocked", step=steps, reason=blocked)
        history.append(msg)
        entry["blocked"] = blocked

    # Frontier observe: bank every unseen link target as pending so
    # the to-do ledger — not the 8-line history window — remembers what
    # still needs reading. Label-keyed: SERP hrefs are opaque redirects.
    if frontier is not None:
        try:
            new_targets = frontier.observe(elements, steps)
            if new_targets:
                entry["frontier_new"] = new_targets
        except Exception:
            pass
    report_mod.write_raw_png(run_dir, steps, raw_png)
    see_line = (
        f"{len(elements)} elements ({n_links} links, {n_ext} external) · tabs={n_tabs} · focused={focused.role or '-'}"
        f"{' (credential)' if focused.is_credential else ''} · "
        f"text={len(page_text.strip())}ch"
    )
    _kv("jev-solver:page:see", elements=len(elements), links=n_links,
        external=n_ext, tabs=n_tabs, focused=focused.role or "-",
        credential=focused.is_credential, text_chars=len(page_text.strip()))
    entry["see"] = see_line
    if page_text.strip():
        log(f"TEXT   {page_text.strip()[:160]}")
    entry["page_text"] = page_text[:400]

    return {"restarted": False, "raw_png": raw_png, "elements": elements,
            "focused": focused, "page_text": page_text, "n_tabs": n_tabs,
            "blocked": blocked, "shot_streak": shot_streak,
            "known_tab_ids": known_tab_ids, "last_page_id": last_page_id,
            "reading_until_step": reading_until_step,
            "stopped": False, "stop_reason": ""}


# ---------------------------------------------------------------------------
# STEER
# ---------------------------------------------------------------------------


async def _step_steer(
    *,
    platform,
    entry: dict,
    steps: int,
    moves: int,
    steer_abs: str,
    steer_consumed: int,
    history: list[str],
    instructions: list[str],
) -> dict:
    """Process new steer-file lines; returns flags + updated counters."""
    from ..run.agent_runner import read_steers
    from ..run.logging_utils import log

    stopped = False
    stop_reason = ""
    paused_delta: bool | None = None
    new_steers, steer_consumed = read_steers(steer_abs, steer_consumed)
    for kind_s, payload in new_steers:
        label = payload if kind_s == "goto" else (payload or "stop")
        # Console shows a capped preview; the full line still lands in
        # the entry. Stale multi-KB steer files used to flood the feed.
        preview = label if len(label) <= 160 else label[:157] + "..."
        _kv("jev-solver:steer:line", kind=kind_s, preview=preview)
        entry.setdefault("steer", []).append(label)
        if kind_s == "stop":
            stopped = True
            stop_reason = "steer stop"
            break
        if kind_s == "pause":
            paused_delta = True
        elif kind_s == "resume":
            paused_delta = False
        if kind_s == "goto":
            res = await goto_url(platform, payload or "")
            _log_result(res)
            history.append(f"step {steps}: steer goto — {res}")
            moves += 1
        elif kind_s == "instruction" and payload:
            instructions.append(payload)
    return {"steer_consumed": steer_consumed, "stopped": stopped,
            "stop_reason": stop_reason,
            "paused_delta": paused_delta, "moves": moves}


# ---------------------------------------------------------------------------
# PROPOSE + DECIDE
# ---------------------------------------------------------------------------


async def _step_propose_decide(
    *,
    machine,
    phase_trail: list[str],
    entry: dict,
    steps: int,
    effective_task: str,
    start_url: str,
    platform,
    page,
    elements: list,
    focused,
    page_text: str,
    history: list[str],
    n_tabs: int,
    notes: list[str],
    visited: list[str],
    lessons,
    blocked,
    reading_until_step: int,
    mismatch_sig: tuple | None,
    mismatch_run: int,
    escalated_sigs: set,
    escalated_urls: set[str],
    last_typed: tuple | None,
    blank_streak: int,
    run_dir: str,
    report_mod,
    frontier=None,
) -> dict:
    """Run PROPOSE then DECIDE; returns the decision + updated bookkeeping.

    On a DECIDE failure the step idles: the machine walks
    see -> decide -> gate -> verify, the entry is recorded, and the
    caller sleeps and continues.
    """
    from ..run.logging_utils import log

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
            reading_note=reading_note, frontier=frontier,
        )
    except Exception as exc:  # noqa: BLE001
        _kv("jev-solver:action:propose", status="failed",
            error=exc.__class__.__name__)
    # Phase clock: captured here (not by the caller) so propose/decide
    # durations are real — a single caller-side timestamp made decide
    # times negative and hid which phase burns the budget (seen live).
    t_proposed = time.time()
    if proposed is not None:
        import textwrap as _tw
        _short_rationale = _tw.shorten(proposed.rationale, width=100,
                                       placeholder="...")
        _kv("jev-solver:action:propose", action=proposed.kind,
            target=(f"#{proposed.item}" if proposed.item is not None else "-"),
            url=proposed.url or "-", rationale=_short_rationale)
        entry["propose"] = (
            f"{proposed.kind} #{proposed.item} : {_short_rationale}"
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
            approval_question=approval_struct, frontier=frontier,
        )
    except Exception as exc:
        _kv("jev-solver:decision:select", status="failed",
            error=exc.__class__.__name__)
        entry["decide"] = f"failed: {exc}"
        history.append(f"step {steps}: decide failed, idled")
        advance(machine, phase_trail, "perceived")  # see -> decide
        advance(machine, phase_trail, "decided")  # decide -> gate
        advance(machine, phase_trail, "idle_now")  # gate -> verify
        entry["phases"] = list(phase_trail)
        report_mod.append_transcript(run_dir, entry)
        return {"idle": True, "t_proposed": t_proposed,
                "t_decided": time.time()}

    conf = decision.confidence
    advance(machine, phase_trail, "perceived")  # see -> decide
    # The model-side settle flag can only add patience, never remove
    # the regex guard: a loading verdict forces wait even on readable
    # text, but a ready verdict never overrides a loading regex.
    ready_now = page_settled(page_text) and decision.page_ready >= 0.35
    _kv("jev-solver:decision:select", action=decision.kind.value,
        conf=round(conf, 2),
        target=(f"#{decision.element_idx}/{decision.item_ref}"
                if decision.element_idx is not None else "-"),
        site=(decision.target_url or "other"
              if decision.kind == Kind.GOTO else "-"),
        ready=round(decision.page_ready, 2),
        needs_text=round(decision.needs_text, 2),
        done=round(decision.task_done, 2), progress=round(decision.progress, 2),
        approval=round(decision.approval, 2), fit=round(decision.fits, 2))
    # Full JEV answer set: every question's verbatim choice + confidence,
    # so the feed shows what the model actually said (not just the
    # decoded verdict). Raw payload stays in step-NN-answers.json.
    try:
        _kv("jev-solver:decision:answers",
            **_jev_answer_summary(getattr(decision, "raw", None)))
    except Exception:  # noqa: BLE001
        pass
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
        executable=proposal_executable(proposed, elements, frontier),
        approval=decision.approval)
    if route == "override":
        _kv("jev-solver:decision:approve", action=proposed.kind,
            target=(f"#{proposed.item}" if proposed.item is not None else "-"),
            approval=round(decision.approval, 2))
        history.append(f"step {steps}: approved proposal — {proposed.rationale[:100]}")
        entry["decide"] += " [approved-override]"
        decision = _apply_proposal(decision, proposed, elements)
        conf = decision.confidence
    elif route == "fallback":
        _kv("jev-solver:decision:approve", action=proposed.kind,
            target=(f"#{proposed.item}" if proposed.item is not None else "-"),
            approval=round(decision.approval, 2), mode="veto-fallback")
        history.append(f"step {steps}: veto fallback — {proposed.rationale[:100]}")
        entry["decide"] += " [veto-fallback]"
        decision = _apply_proposal(decision, proposed, elements)
        conf = decision.confidence
    elif route == "mismatch-idle":
        m_sig = (
            decision.kind.value, decision.element_idx,
            next(((e.label or e.text or "")[:40] for e in elements
                  if e.idx == decision.element_idx), ""),
            next((perception.host_of(e.href) for e in elements
                  if e.idx == decision.element_idx), ""),
            page.url,
        )
        if m_sig == mismatch_sig:
            mismatch_run += 1
        else:
            mismatch_sig, mismatch_run = m_sig, 1
        _kv("jev-solver:decision:mismatch", action=decision.kind.value,
            target=f"#{decision.element_idx}", fit=round(decision.fits, 2),
            count=mismatch_run)
        history.append(f"step {steps}: pick mismatched its verb (fit={decision.fits:.2f}), waited")
        entry["act"] = f"mismatch wait ({decision.kind.value} #{decision.element_idx})"
        esc_url = None
        if mismatch_run >= 3 and m_sig not in escalated_sigs:
            escalated_sigs.add(m_sig)
            cand = await propose_url(task=effective_task, history=history)
            esc_url = escalation_target(
                mismatch_run,
                cand.url if cand.ok else None,
                page.url, escalated_urls)
        if esc_url is not None:
            escalated_urls.add(perception.norm_url(esc_url))
            _kv("jev-solver:gate:escalate", url=esc_url)
            history.append(f"step {steps}: escalated to goto {esc_url}")
            entry["decide"] += " [escalated-goto]"
            decision = replace(decision, kind=Kind.GOTO, element_idx=None,
                               target_url=esc_url, propose_url=False,
                               confidence=0.99)
        else:
            entry["noop_bump"] = 1 if mismatch_run >= 3 else 0
            decision = replace(decision, kind=Kind.WAIT, element_idx=None)
    # Retype intent on an already-filled field means submit: the query
    # is in the box, typing it again changes nothing — Enter does.
    # Snapshot fill-state lies on some pages (Google hides the typed
    # query when suggestions render), so a same-box repeat also reads
    # the live DOM value before falling through to a retype.
    if (decision.kind == Kind.TYPE_AT and decision.element_idx is not None
            and last_typed is not None
            and (decision.element_idx, page.url) == last_typed):
        el0 = next((e for e in elements
                    if e.idx == decision.element_idx), None)
        live_value = ""
        if el0 is not None and el0.value_len == 0 and el0.aria:
            live_value = await read_input_value(platform, el0.aria)
        if should_submit_instead(decision.element_idx, elements,
                                 last_typed, page.url, live_value):
            _kv("jev-solver:gate:retype",
                target=f"#{decision.element_idx}")
            history.append(f"step {steps}: retype on filled field, submitting instead")
            entry["decide"] += " [retype-submit]"
            decision = replace(decision, kind=Kind.PRESS_ENTER, element_idx=None)
            last_typed = None
    # Blank-page recovery: a dead load draws confident waits forever
    # (seen live: 14 straight waits on an empty tab). Every third
    # blank step with an idle verdict becomes a refresh instead.
    if (blank_streak >= 3 and blank_streak % 3 == 0
            and decision.kind in (Kind.WAIT, Kind.NONE)):
        _kv("jev-solver:gate:blank-refresh", streak=blank_streak)
        history.append(f"step {steps}: blank page x{blank_streak}, refreshing instead of waiting")
        entry["decide"] += " [blank-refresh]"
        decision = replace(decision, kind=Kind.REFRESH, element_idx=None)
    report_mod.write_answers_json(run_dir, steps, decision.raw)

    return {"idle": False, "decision": decision, "conf": conf,
            "ready_now": ready_now, "proposed": proposed,
            "mismatch_sig": mismatch_sig, "mismatch_run": mismatch_run,
            "last_typed": last_typed,
            "noop_bump": entry.get("noop_bump", 0),
            "t_proposed": t_proposed, "t_decided": time.time()}


# ---------------------------------------------------------------------------
# GATE + ACT (the big branching)
# ---------------------------------------------------------------------------


def _log_challenge_result(res: str, steps: int, idx: int) -> dict | None:
    """Console-print a challenge_control result without the inline blob.

    Returns the parsed shield-bypass-attempt record (or None). The full ``res``
    (with machine tail) still goes to entry/transcript for audit; the
    console gets the short verdict plus the record as formatted JSON
    (crawl4ai extracted_content convention). A 500-char inline JSON blob
    on one RESULT line is unreadable on Windows consoles.
    """
    from .._log_sink import emit_json as _emit_json
    from ..capability.shield_solve import unpack_attempt_tail as _unpack
    try:
        attempt = _unpack(res)
    except Exception:
        attempt = None
    if attempt:
        _kv("jev-solver:result:update",
            summary=_short_result(res.split("shield-bypass-attempt:")[0]))
        _emit_json({"event": "shield-bypass-attempt", "step": steps,
                    "idx": idx, "record": attempt})
    else:
        _log_result(res)
    return attempt


def _jev_answer_summary(raw: Any) -> dict[str, str]:
    """Per-question ``choice:confidence`` from a raw JEV payload.

    Pure (nothing but the dict): unit-testable without a browser. Skips
    non-question entries (progress score) and anything malformed.
    """
    answers = (raw or {}).get("answers", {}) or {}
    out: dict[str, str] = {}
    for qid, ans in answers.items():
        if not isinstance(ans, dict) or qid == "progress":
            continue
        try:
            conf = round(float(ans.get("confidence", 0.0) or 0.0), 2)
        except (ValueError, TypeError):
            conf = 0.0
        out[str(qid)] = f"{ans.get('choice', '-')}:{conf}"
    return out


RANK_MARKER = "solver-rank:"


def _parse_rank_tail(res: str) -> dict | None:
    """Extract the solver-rank record from a result-line tail.

    Actions appends ``solver-rank:{order,rates}`` to checkbox-path
    returns (same machine-tail pattern as shield-bypass-attempt): the ranked
    backend order plus the ledger rates behind it. None when absent.
    """
    import json as _json

    try:
        start = str(res or "").index(RANK_MARKER) + len(RANK_MARKER)
    except ValueError:
        return None
    tail = str(res)[start:].strip()
    depth = 0
    for i, ch in enumerate(tail):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = _json.loads(tail[:i + 1])
                except ValueError:
                    return None
                if isinstance(obj, dict) and isinstance(
                        obj.get("order"), list):
                    return obj
                return None
    return None


def _short_result(res: str) -> str:
    """Console summary of an action result: JSON cut, verdicts kept.

    Packed lines read ``<head> MARKER{json} <tail-summary>`` — the tail
    carries the human verdict (conf/text/awaiting-review) while the
    JSON block prints separately via emit_json. Full ``res`` still
    lands in entry + transcript + history.
    """
    text = str(res or "")
    # Solver-rank tail is metadata, never console content: drop it
    # before the verdict scan (it trails every other packed block).
    if RANK_MARKER in text:
        text = text.split(RANK_MARKER)[0].rstrip()
    for marker in ("shield-bypass-attempt:", "shield-bypass:", "ddddocr:",
                   "2captcha-python:", "captchakraken:"):
        if marker not in text:
            continue
        head, rest = text.split(marker, 1)
        start = rest.find("{")
        tail = ""
        if start >= 0:
            depth = 0
            for i, ch in enumerate(rest[start:]):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        tail = rest[start + i + 1:]
                        break
        text = (head + " " + tail).strip()
        break
    text = " ".join(text.split())
    return (text[:157] + "...") if len(text) > 160 else (text or "(empty)")


def _log_result(res: str) -> None:
    """Console-print an action result as one kv summary line."""
    _kv("jev-solver:result:update", summary=_short_result(res))


async def _step_gate_act(
    *,
    machine,
    phase_trail: list[str],
    entry: dict,
    steps: int,
    decision,
    conf: float,
    min_confidence: float,
    ready_now: bool,
    elements: list,
    page,
    platform,
    noops: int,
    moves: int,
    last_sig: tuple | None,
    sig_run: int,
    fix_limit: int,
    last_typed: tuple | None,
    last_effect_kind: str | None,
    blank_streak: int,
    page_text: str,
    history: list[str],
    effective_task: str,
    start_url: str,
    run_dir: str,
    recoveries: list,
    recover_cap: int,
    notes: list[str],
) -> dict:
    """Run GATE + ACT; returns the acted flags and updated counters.

    The machine advances gate -> act -> heal/verify as the handlers
    dispatch. The caller owns the post-act re-sync (page rebind,
    t_acted_at timing) and the reporting that follows.
    """
    from ..run.logging_utils import log

    acted = False
    stopped = False
    stop_reason = ""
    done = False
    # Loop-guard: the tab, clear, and click systems all report into
    # the decision, but a confident classifier can still fixate
    # (same click 5x). Three identical click/type targets in a row
    # force a wait here instead of executing again.
    guard_trip = False
    if decision.kind in (Kind.CLICK_ITEM, Kind.TYPE_AT, Kind.CHALLENGE) and decision.element_idx is not None:
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
    advance(machine, phase_trail, "decided")  # decide -> gate
    # Confidence gates risky actions only: back/refresh/escape cannot
    # type, post, pay, or navigate externally, so gating them strands
    # the loop (seen live: low-conf back idled 3x and killed a run
    # that had more pages to read). DONE is always exempt.
    if confidence_gated(decision.kind, conf, min_confidence):
        _kv("jev-solver:gate:idle", conf=round(conf, 2),
            min_conf=min_confidence, noop=noops + 1)
        history.append(f"step {steps}: low conf {conf:.2f}, idled")
        entry["act"] = "idle (low confidence)"
        noops += 1
    elif guard_trip:
        # Forced wait is a correction, not doubt: neutral like wait,
        # so the guard's own protection can't kill the run via the
        # no-op counter. Genuine fixation (6x same target) stops
        # honestly with its own reason instead.
        _kv("jev-solver:guard:loopguard", action=decision.kind.value,
            target=f"#{decision.element_idx}", count=sig_run)
        history.append(f"step {steps}: loopguard tripped on {decision.kind.value} #{decision.element_idx}, waited")
        entry["act"] = f"loopguard wait ({decision.kind.value} #{decision.element_idx})"
        if sig_run >= fix_limit:
            stopped = True
            stop_reason = (f"loopguard fixation on {decision.kind.value} "
                           f"#{decision.element_idx} x{sig_run}")
            advance(machine, phase_trail, "abort")  # gate -> stopped
            _kv("jev-solver:run:stop", reason=stop_reason)
    elif decision.kind == Kind.WAIT:
        # Patience, not doubt: the screen is still loading. Neutral —
        # it neither resets nor advances the no-op count, so a slow
        # page can't trigger the two-no-op stop by itself.
        _kv("jev-solver:gate:idle", mode="wait")
        history.append(f"step {steps}: wait")
        entry["act"] = "wait"
    elif decision.kind == Kind.NONE:
        _kv("jev-solver:gate:idle", mode="none", noop=noops + 1)
        history.append(f"step {steps}: none")
        entry["act"] = "none"
        noops += 1
    elif decision.kind == Kind.DONE:
        if not ready_now:
            msg = (f"step {steps}: DONE REJECTED (page not settled — "
                   f"regex={page_settled(page_text)}, model ready={decision.page_ready:.2f}) — "
                   "wait one step and re-observe")
            _kv("jev-solver:gate:done", status="denied",
                reason="page-not-settled")
            history.append(msg)
            entry["act"] = "DONE rejected (page not settled)"
        elif decision.task_done < 0.5:
            msg = (f"step {steps}: DONE REJECTED (model completion flag "
                   f"{decision.task_done:.2f} < 0.5) — keep working")
            _kv("jev-solver:gate:done", status="denied",
                reason="model-flag")
            history.append(msg)
            entry["act"] = "DONE rejected (model flag)"
        elif not page_text.strip():
            msg = f"step {steps}: DONE REJECTED (empty page text)"
            _kv("jev-solver:gate:done", status="denied",
                reason="empty-page")
            history.append(msg)
            entry["act"] = "DONE rejected (empty page)"
        else:
            full_note = " | ".join(notes[-3:]) or page_text.strip()
            note = full_note[:300]
            done = True
            advance(machine, phase_trail, "finish")  # gate -> done
            entry["act"] = f"DONE ({note[:80]})"
            history.append(f"step {steps}: DONE — {note[:80]}")
            _kv("jev-solver:gate:done", status="accepted", note=note[:120])
    elif decision.kind == Kind.GOTO:
        if reading_cooldown_active(steps, entry.get("_reading_until_step", 0)):
            msg = (f"step {steps}: GOTO REJECTED (reading cooldown to step "
                   f"{entry.get('_reading_until_step', 0)}) — read the adopted result page first")
            _kv("jev-solver:gate:goto", status="denied",
                reason="reading-cooldown")
            history.append(msg)
            entry["act"] = "goto rejected (reading)"
        else:
            target = decision.target_url
            if decision.propose_url or not target:
                proposed = await propose_url(task=effective_task, history=history)
                target = proposed.url if proposed.ok else None
            if not target:
                _kv("jev-solver:gate:goto", status="denied",
                    reason="no-url")
                history.append(f"step {steps}: goto without URL, idled")
                entry["act"] = "goto (no URL)"
                noops += 1
            else:
                _kv("jev-solver:action:execute", action="goto", url=target)
                res = await goto_url(platform, target)
                _log_result(res)
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
        _kv("jev-solver:action:execute", action="press_enter")
        res = await press_key(platform, "Enter")
        _log_result(res)
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
        _kv("jev-solver:action:execute", action="press_escape")
        res = await press_key(platform, "Escape")
        _log_result(res)
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
        _kv("jev-solver:action:execute", action="refresh")
        res = await platform.refresh_page()
        _log_result(res)
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
        _kv("jev-solver:action:execute", action="back")
        res = await platform.go_back()
        _log_result(res)
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
        _kv("jev-solver:action:execute", action="close_others")
        n_closed = await platform.close_other_tabs()
        res = f"closed {n_closed} other tab(s)"
        _log_result(res)
        entry["act"] = "close_others"
        entry["result"] = res
        history.append(f"step {steps}: {res}")
        moves += 1
        noops = 0
        acted = True
    elif decision.kind == Kind.CHALLENGE:
        idx = decision.element_idx
        by_idx = {e.idx: e for e in elements}
        if idx is None or idx not in by_idx:
            _kv("jev-solver:action:execute", action="challenge",
                status="no-item")
            history.append(f"step {steps}: challenge without item, idled")
            entry["act"] = "challenge (no item)"
            noops += 1
        else:
            _kv("jev-solver:action:execute", action="challenge",
                target=f"#{idx}")
            res = await challenge_control(platform, elements, idx,
                                          expected_kind=by_idx[idx].kind)
            # Failed shield attempts carry a machine tail that the
            # failure rails would otherwise swallow: parse it into the
            # audit block WITHOUT changing verdict accounting (the
            # error prefix still drives escalate/fail paths below).
            _attempt = _log_challenge_result(res, steps, idx)
            if _attempt:
                entry["shield"] = _attempt
                entry["shield_idx"] = idx
            # Solver ranking rides into the audit block AND history
            # text: JEV reasons over past backend effectiveness with
            # its existing machinery (history is model-visible).
            _rank = _parse_rank_tail(res)
            if _rank:
                entry["solver_rank"] = _rank
                _kv("jev-solver:decision:solvers",
                    ranked=">".join(
                        f"{s}({_rank.get('rates', {}).get(s, '-')})"
                        for s in _rank.get("order", [])))
                history.append(
                    f"step {steps}: solvers ranked "
                    + " > ".join(
                        f"{s}({_rank.get('rates', {}).get(s, '-')})"
                        for s in _rank.get("order", [])))
                # Solver effectiveness joins the persistent notes so EVERY
                # future JEV decision reasons over past backend results,
                # not just the last 8 history lines.
                _rank_note = (
                    "solvers by past success: " + ", ".join(
                        f"{s}={_rank.get('rates', {}).get(s, '-')}"
                        for s in _rank.get("order", [])))
                if not any(n.startswith("solvers by past success:") for n in notes):
                    notes.append(_rank_note)
                else:
                    notes[:] = [n if not n.startswith(
                        "solvers by past success:") else _rank_note
                        for n in notes]
                notes[:] = perception.trim_notes(notes)
            if "ddddocr:" in res:
                # ddddocr pipeline result: analysis only, no dispatch yet.
                # Record the structured payload for JSONL audit; the OCR
                # text + suggestion ride in history so the NEXT step's
                # normal propose/decide path (JEV) reviews them before any
                # typing happens. Counted as progress with a dedicated
                # effect kind that is deliberately NOT in EFFECT_KINDS,
                # so the no-effect rail never trips on an analysis step.
                from ..capability.captcha_ocr import unpack_result_line

                ocr_record = unpack_result_line(res) or {}
                # JEV review BEFORE any interaction: score the ranked OCR
                # candidates against the instruction; the pick (not the
                # raw argmax) is what the next step may type.
                pick_text, pick_conf = await decide_captcha_action(
                    instruction=str(ocr_record.get("instruction", "")),
                    candidates=ocr_record.get("candidates", []),
                    page_excerpt=page_text)
                ocr_record["pick"] = {"text": pick_text,
                                       "confidence": round(pick_conf, 3)}
                entry["act"] = f"element #{idx} challenge"
                entry["result"] = res
                entry["captcha_ocr"] = ocr_record
                entry["captcha_idx"] = idx
                sugg = ""
                if (pick_text and ocr_record.get("suggest_kind")
                        and ocr_record.get("suggest_item") is not None):
                    sugg = (f" — JEV pick {pick_text[:40]!r} "
                            f"(conf={pick_conf:.2f}) → suggest "
                            f"{ocr_record['suggest_kind']} "
                            f"#{ocr_record['suggest_item']}")
                elif ocr_record.get("suggest_kind") and ocr_record.get("suggest_item") is not None:
                    sugg = (f" — suggest {ocr_record['suggest_kind']} "
                            f"#{ocr_record['suggest_item']} "
                            f"text={str(ocr_record.get('text', ''))[:40]!r} "
                            f"(JEV abstained)")
                history.append(
                    f"step {steps}: ddddocr #{idx} "
                    f"conf={float(ocr_record.get('confidence', 0.0) or 0.0):.2f}"
                    f"{sugg}")
                _kv("capability:ddddocr:done", target=f"#{idx}",
                    conf=round(float(
                        ocr_record.get("confidence", 0.0) or 0.0), 2))
                # Structured OTC response as formatted JSON (crawl4ai
                # extracted_content convention): the model infers the
                # record from this block, not from the prose summary.
                from .._log_sink import emit_json as _emit_json
                _emit_json({"event": "ddddocr", "step": steps,
                            "idx": idx, "record": ocr_record})
                moves += 1
                noops = 0
                acted = True
                last_effect_kind = "ddddocr"
            elif "shield-bypass:" in res:
                # Native shield solve dispatched and verified by token
                # poll (not by a body-text diff). Record the structured
                # payload for JSONL audit; the verdict rides in history
                # so the NEXT step's normal propose/decide path (JEV)
                # reads the post-solve page. Dedicated effect kind that
                # is deliberately NOT in EFFECT_KINDS, so the no-effect
                # rail never trips on a token-verified solve.
                from ..capability.shield_solve import unpack_result_line as _unpack_shield

                shield_record = _unpack_shield(res) or {}
                entry["act"] = f"element #{idx} challenge"
                entry["result"] = res
                entry["shield"] = shield_record
                entry["shield_idx"] = idx
                ok = bool(shield_record.get("success"))
                history.append(
                    f"step {steps}: shield-bypass #{idx} "
                    f"{shield_record.get('challenge_type', '?')} "
                    f"success={ok} "
                    f"token_len={int(shield_record.get('token_len', 0) or 0)}")
                _kv("capability:shield-bypass:done", target=f"#{idx}",
                    family=shield_record.get("challenge_type", "?"),
                    success=ok,
                    token_len=int(
                        shield_record.get("token_len", 0) or 0))
                # Structured OTC response as formatted JSON (crawl4ai
                # extracted_content convention): the model infers the
                # record — including the solver's in-response logs —
                # from this block, not from the prose summary.
                from .._log_sink import emit_json as _emit_json
                _emit_json({"event": "shield-bypass", "step": steps,
                            "idx": idx, "record": shield_record})
                moves += 1
                noops = 0
                acted = True
                last_effect_kind = "shield-bypass"
            elif "2captcha-python:" in res:
                # Paid 2captcha solve dispatched and token injected.
                # Same audit contract as shield-bypass: structured payload
                # for JSONL, verdict in history, dedicated effect kind
                # outside EFFECT_KINDS (token-verified, not a body diff).
                from ..capability.twocaptcha_client import (
                    unpack_result_line as _unpack_tc)

                tc_record = _unpack_tc(res) or {}
                entry["act"] = f"element #{idx} challenge"
                entry["result"] = res
                entry["twocaptcha"] = tc_record
                entry["twocaptcha_idx"] = idx
                ok = bool(tc_record.get("success"))
                history.append(
                    f"step {steps}: 2captcha-python #{idx} "
                    f"{tc_record.get('captcha_type', '?')} "
                    f"success={ok} "
                    f"token_len={int(tc_record.get('token_len', 0) or 0)}")
                _kv("capability:2captcha-python:done", target=f"#{idx}",
                    family=tc_record.get("captcha_type", "?"), success=ok,
                    token_len=int(tc_record.get("token_len", 0) or 0))
                from .._log_sink import emit_json as _emit_json
                _emit_json({"event": "2captcha-python", "step": steps,
                            "idx": idx, "record": tc_record})
                moves += 1
                noops = 0
                acted = True
                last_effect_kind = "2captcha-python"
            elif "captchakraken:" in res:
                # Hosted vision grid solve (rounds of tile clicks +
                # verify gate). Same audit contract as shield-bypass:
                # structured payload for JSONL, verdict in history,
                # dedicated effect kind outside EFFECT_KINDS.
                from ..capability.grid_solve import (
                    unpack_result_line as _unpack_grid)

                grid_record = _unpack_grid(res) or {}
                entry["act"] = f"element #{idx} challenge"
                entry["result"] = res
                entry["grid"] = grid_record
                entry["grid_idx"] = idx
                ok = bool(grid_record.get("success"))
                history.append(
                    f"step {steps}: captchakraken #{idx} "
                    f"success={ok} "
                    f"rounds={int(grid_record.get('rounds', 0) or 0)} "
                    f"tiles={grid_record.get('tiles_clicked', [])}")
                _kv("captcha:vision_grid:done", target=f"#{idx}",
                    success=ok,
                    rounds=int(grid_record.get("rounds", 0) or 0),
                    tiles_clicked=grid_record.get("tiles_clicked", []))
                from .._log_sink import emit_json as _emit_json
                _emit_json({"event": "captchakraken", "step": steps,
                            "idx": idx, "record": grid_record})
                moves += 1
                noops = 0
                acted = True
                last_effect_kind = "captchakraken"
            elif "escalating" in res:
                # Image/puzzle CAPTCHA: TRY, don't stop — checkbox
                # attempts, waits, and re-observes continue; only a
                # sustained siege (streak at cap) ends the run.
                # Neutral like wait: a blocked page is environmental,
                # not doubt, so it never feeds the no-op counter.
                history.append(f"step {steps}: image challenge — {res}")
                entry["act"] = f"element #{idx} challenge"
                entry["result"] = res
                entry["step_captcha"] = True
                entry["captcha_streak"] = entry.get("captcha_streak", 0) + 1
                history.append(f"step {steps}: image challenge dead-end x{entry['captcha_streak']} — consider alternate routes (other engine, direct URL)")
                _kv("capability:challenge:deadend",
                    streak=entry["captcha_streak"], cap=CAPTCHA_MAX_ATTEMPTS)
            elif action_failed(res):
                history.append(f"step {steps}: challenge failed — {res}")
                entry["act"] = f"element #{idx} challenge"
                entry["result"] = res
                noops += 1
            else:
                entry["act"] = f"element #{idx} challenge"
                entry["result"] = res
                history.append(f"step {steps}: worked challenge #{idx} — {res}")
                moves += 1
                noops = 0
                acted = True
                last_effect_kind = "challenge"
    else:  # CLICK_ITEM / TYPE_AT
        idx = decision.element_idx
        by_idx = {e.idx: e for e in elements}
        if idx is None or idx not in by_idx:
            _kv("jev-solver:action:execute", action=decision.kind.value,
                status="no-item")
            history.append(f"step {steps}: {decision.kind.value} without item, idled")
            entry["act"] = f"{decision.kind.value} (no item)"
            noops += 1
        elif decision.kind == Kind.CLICK_ITEM:
            target = by_idx[idx]
            if target.kind in BARE_CLICK_VETO_KINDS:
                msg = (f"step {steps}: vetoed bare click on {target.kind} #{idx} — "
                       "text fields are typed (type_at), never bare-clicked")
                _kv("jev-solver:gate:veto", detail=msg[:160])
                history.append(msg)
                entry["act"] = f"click vetoed ({target.kind} #{idx})"
                noops += 1
            else:
                _kv("jev-solver:action:execute", action="click_item",
                    target=f"#{idx}")
                res = await click_item(platform, elements, idx,
                                        expected_kind=by_idx[idx].kind)
                _log_result(res)
                # Self-healing retry: a stale map, covered target, or
                # vanished box detours ACT -> HEAL -> ACT for exactly
                # one re-attempt (remapped by aria/selector/label),
                # else ACT -> HEAL -> VERIFY. Jev triages the failure
                # first; the runner executes the strategy (remap /
                # dismiss / challenge / abort).
                if action_failed(res) and ("stale map" in res
                                           or "target covered" in res
                                           or "has no bounding box" in res
                                           or "dispatch failed" in res):
                    old_label = (target.label or target.placeholder
                                 or target.text or target.id or "")
                    _kv("jev-solver:heal:triage", error=_short_result(res))
                    history.append(f"step {steps}: click failed, healing — {res[:90]}")
                    advance(machine, phase_trail, "act_now")  # gate -> act
                    advance(machine, phase_trail, "heal_needed")  # act -> heal
                    strategy, need_new = await decide_heal_action(
                        error_msg=res, last_kind="click_item",
                        page_text=page_text, target_kind=target.kind)
                    _kv("jev-solver:heal:strategy", strategy=strategy.value,
                        novelty=round(need_new, 2))
                    history.append(f"step {steps}: heal triage -> {strategy.value}")
                    heal_edge = None
                    heal_abort = None
                    heal_accounted = False
                    if strategy == HealStrategy.ABORT:
                        heal_abort = (f"step {steps}: heal triage aborted — "
                                      f"{res[:120]}")
                        history.append(heal_abort)
                        machine.healed_target_ready_flag = False
                        advance(machine, phase_trail, "heal_failed")
                        heal_edge = "heal_failed"
                    elif strategy in (HealStrategy.DRAG_SLIDER,
                                      HealStrategy.SOLVE_CHALLENGE):
                        from .helpers import heal_strategy_fits as _fits

                        if not _fits(strategy.value, target.kind,
                                     old_label):
                            _kv("jev-solver:heal:route",
                                strategy="unfit-challenge",
                                want=strategy.value,
                                kind=target.kind)
                            history.append(
                                f"step {steps}: heal {strategy.value} "
                                f"does not fit {target.kind} #{idx} — "
                                f"skipping challenge dispatch")
                            machine.healed_target_ready_flag = False
                            advance(machine, phase_trail, "heal_failed")
                            heal_edge = "heal_failed"
                        else:
                            _kv("jev-solver:heal:route", strategy="challenge")
                            res = await challenge_control(
                                platform, elements, idx,
                                expected_kind=target.kind)
                            _heal_attempt = _log_challenge_result(
                                res, steps, idx)
                            if _heal_attempt:
                                entry["shield"] = _attempt
                                entry["shield_idx"] = idx
                            if action_failed(res):
                                history.append(f"step {steps}: heal challenge failed — {res[:90]}")
                                machine.healed_target_ready_flag = False
                                advance(machine, phase_trail, "heal_failed")
                                heal_edge = "heal_failed"
                            else:
                                entry["act"] = f"element #{idx} challenge (via heal)"
                                entry["result"] = res
                                history.append(f"step {steps}: heal worked challenge #{idx} — {res[:90]}")
                                moves += 1
                                noops = 0
                                acted = True
                                last_effect_kind = "challenge"
                                heal_accounted = True
                                machine.healed_target_ready_flag = True
                                advance(machine, phase_trail, "healed")
                                heal_edge = "healed"
                    elif strategy == HealStrategy.DISMISS_COVER:
                        try:
                            await platform.page.keyboard.press("Escape")
                            await asyncio.sleep(0.6)
                        except Exception:
                            pass
                        _kv("jev-solver:heal:route", strategy="dismiss-cover")
                        res = await click_item(platform, elements, idx,
                                               expected_kind=target.kind)
                        _log_result(res)
                        if action_failed(res):
                            machine.healed_target_ready_flag = False
                            advance(machine, phase_trail, "heal_failed")
                            heal_edge = "heal_failed"
                        else:
                            machine.healed_target_ready_flag = True
                            advance(machine, phase_trail, "healed")
                            heal_edge = "healed"
                    elif strategy == HealStrategy.EXPAND_CAPABILITY:
                        # Live synthesis (gated): compose -> validate
                        # (AST + signature + dry-run) -> register ->
                        # execute once. Any gate failure accounts as
                        # heal_failed; the code is audited to run_dir.
                        cap_name = f"heal_step{steps}"
                        box_norm = (list(target.box)
                                    if target.box is not None else None)
                        code = await synthesize_capability(
                            issue=res, ref=target.ref,
                            kind=target.kind, label=old_label,
                            box_norm=box_norm, page_text=page_text)
                        if code is None:
                            history.append(f"step {steps}: heal synthesis declined")
                            _kv("jev-solver:heal:route", strategy="synthesis-declined")
                            machine.healed_target_ready_flag = False
                            advance(machine, phase_trail, "heal_failed")
                            heal_edge = "heal_failed"
                        else:
                            audit_path = os.path.join(
                                run_dir, f"heal-{steps:02d}-{cap_name}.py")
                            try:
                                with open(audit_path, "w", encoding="utf-8") as f:
                                    f.write(code)
                            except OSError:
                                audit_path = "(audit write failed)"
                            _kv("jev-solver:heal:synthesized", capability=cap_name,
                                audit=audit_path)
                            ctx = {"ref": target.ref, "role": target.kind,
                                   "label": old_label, "box_norm": box_norm}
                            result = await validate_capability(
                                code, cap_name, platform,
                                target.ref, ctx)
                            if not result.valid:
                                history.append(f"step {steps}: healed code rejected — {result.error}")
                                _kv("jev-solver:heal:route", strategy="validation-failed",
                                error=str(result.error)[:80])
                                machine.healed_target_ready_flag = False
                                advance(machine, phase_trail, "heal_failed")
                                heal_edge = "heal_failed"
                            else:
                                register_capability(cap_name, result.func)
                                try:
                                    heal_res = await asyncio.wait_for(
                                        result.func(platform, target.ref, ctx),
                                        timeout=30.0)
                                except asyncio.TimeoutError:
                                    heal_res = "error: healed action timed out after 30s"
                                except Exception as exc:
                                    heal_res = f"error: healed action raised: {exc}"
                                _log_result(heal_res)
                                res = heal_res
                                if action_failed(heal_res):
                                    history.append(f"step {steps}: healed action failed — {heal_res[:90]}")
                                    machine.healed_target_ready_flag = False
                                    advance(machine, phase_trail, "heal_failed")
                                    heal_edge = "heal_failed"
                                else:
                                    entry["act"] = f"{cap_name} ({target.ref})"
                                    entry["result"] = heal_res
                                    history.append(f"step {steps}: healed action worked — {heal_res[:90]}")
                                    moves += 1
                                    noops = 0
                                    acted = True
                                    last_effect_kind = cap_name
                                    heal_accounted = True
                                    machine.healed_target_ready_flag = True
                                    advance(machine, phase_trail, "healed")
                                    heal_edge = "healed"
                    else:  # REMAP_STALE
                        fresh, new_idx = await heal_target(
                            platform, old_label,
                            target.kind, target.sel, target.aria)
                        if new_idx is not None and new_idx != idx:
                            _kv("jev-solver:heal:route", strategy="remap",
                                src=f"#{idx}", dst=f"#{new_idx}")
                            history.append(f"step {steps}: healed #{idx} -> #{new_idx}, retrying")
                            # Retry against the FRESH list: refs and
                            # selectors belong to their own probe.
                            res = await click_item(platform, fresh, new_idx,
                                                   expected_kind=target.kind)
                            _log_result(res)
                            machine.healed_target_ready_flag = True
                            advance(machine, phase_trail, "healed")  # heal -> act
                            heal_edge = "healed"
                            idx = new_idx
                        else:
                            _kv("jev-solver:heal:route", strategy="no-remap")
                            history.append(f"step {steps}: heal found no remap target")
                            machine.healed_target_ready_flag = False
                            advance(machine, phase_trail, "heal_failed")  # heal -> verify
                            heal_edge = "heal_failed"
                    entry["heal_edge"] = heal_edge
                    entry["heal_abort"] = heal_abort
                    entry["heal_accounted"] = heal_accounted
                if action_failed(res):
                    history.append(f"step {steps}: click failed — {res}")
                    entry["act"] = f"element #{idx} click"
                    entry["result"] = res
                    noops += 1
                elif entry.get("heal_accounted"):
                    pass  # challenge-via-heal accounted above
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
            _kv("jev-solver:action:execute", action="type_at",
                target=f"#{idx}", credential=cred)
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
                _log_result(res)
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

    return {"acted": acted, "stopped": stopped, "stop_reason": stop_reason,
            "done": done, "noops": noops, "moves": moves,
            "last_sig": last_sig, "sig_run": sig_run,
            "last_typed": last_typed, "last_effect_kind": last_effect_kind}


# ---------------------------------------------------------------------------
# VERIFY + RECOVER
# ---------------------------------------------------------------------------


async def _step_verify_recover(
    *,
    machine,
    phase_trail: list[str],
    entry: dict,
    steps: int,
    page,
    page_text: str,
    elements: list,
    blocked,
    acted: bool,
    done: bool,
    stopped: bool,
    stop_reason: str,
    noops: int,
    noop_limit: int,
    dead_run: int,
    dead_limit: int,
    prev_fp: tuple | None,
    prev_acted: bool,
    last_effect_kind: str | None,
    recoveries: list,
    recover_cap: int,
    same_fp: bool,
    synth_attempted: bool,
    captcha_streak: int,
    step_captcha: bool,
    heal_edge: str | None,
    heal_abort: str | None,
    notes: list[str],
    effective_task: str,
    run_dir: str,
    report_mod,
    history: list[str],
    platform,
) -> dict:
    """Run VERIFY + RECOVER + stop rails; returns the updated counters.

    The machine walks verify -> recover -> see (recovered) or
    verify -> stopped (any of the stop rails). Only a step that
    ACTED can prove the previous action dead: idle/wait/gate steps
    are patience, not evidence.
    """
    from ..run.logging_utils import log

    # A step that arrives already terminal (DONE accepted or a GATE stop
    # such as loopguard fixation) must not advance the machine: it sits
    # in done/stopped, which have no outgoing edges, so any advance
    # raises TransitionNotAllowed (seen live: gate-abort then
    # verify idle_now crashed the run). Counters below still update;
    # only the transitions are skipped.
    if not done and not stopped:
        if heal_edge == "healed":
            # Heal cycle re-attempted: the engine sits at act, so
            # verify from there whether the retry worked or not.
            advance(machine, phase_trail, "acted")  # act -> verify
        elif heal_edge == "heal_failed":
            pass  # engine already at verify via heal_failed
        else:
            if acted:
                advance(machine, phase_trail, "act_now")  # gate -> act
            # gate -> verify skips act; act -> verify closes it.
            advance(machine, phase_trail, "acted" if acted else "idle_now")

    # VERIFY: fingerprint the step's outcome and evaluate the stop
    # rules. A settled page identical to the previous step means the
    # last action changed nothing readable. Twice in a row ends the
    # run with an honest reason instead of looping to max_steps.
    fp = perception.page_fingerprint(page.url, page_text)
    if no_effect_trip(prev_acted, prev_fp, fp, page_settled(page_text),
                     last_effect_kind):
        dead_run += 1
        history.append(f"step {steps}: no observable effect from {last_effect_kind} x{dead_run}")
        _kv("jev-solver:verify:noeffect", kind=last_effect_kind,
            count=dead_run)
        if dead_run >= dead_limit and not done and not stopped:
            stopped = True
            stop_reason = f"action had no observable effect x{dead_run}"
            advance(machine, phase_trail, "abort")  # verify -> stopped
    else:
        dead_run = 0
    same_fp = prev_fp is not None and fp == prev_fp
    prev_fp = fp
    prev_acted = acted

    # RECOVER: a noop step with budget left re-enters through the
    # recover state for exactly one compensating dispatch — observe
    # (fresh SEE above), classify, select, execute, then continue.
    # Heuristics own the clear-cut cases outright; Jev tie-breaks
    # the ambiguous ones (unknown/failed with changed print).
    # Never a blind repeat. Bounded by recover_cap; every attempt
    # is audited below and lands in the transcript.
    recovered = False
    if (not done and not stopped and not acted
            and len(recoveries) < recover_cap):
        reason = classify_noop(
            str(entry.get("act") or ""), entry.get("result"),
            blank=is_blank_page(elements, page_text),
            blocked=blocked, same_fp=same_fp)
        strategy = select_recovery(reason)
        triaged_by = "heuristic"
        if strategy is None and reason in ("unknown", "failed"):
            choice, conf = await decide_recovery_action(
                reason=reason, act=str(entry.get("act") or ""),
                result=str(entry.get("result") or ""),
                page_excerpt=page_text)
            if choice != "none" and conf >= 0.5:
                strategy = choice
                triaged_by = f"jev({conf:.2f})"
            _kv("jev-solver:recover:triage", strategy=choice,
                conf=round(conf, 2))
        if strategy is not None:
            # Capture pre-recovery state so we can detect whether the
            # compensation actually changed anything on the page.
            pre_rec_url = page.url
            try:
                pre_rec_fp = perception.page_fingerprint(pre_rec_url,
                                                         page_text)
            except Exception:  # noqa: BLE001
                pre_rec_fp = None
            advance(machine, phase_trail, "recover_needed")  # verify -> recover
            rec_result = await execute_recovery(platform, strategy)
            history.append(f"step {steps}: recover {strategy} ({reason}/{triaged_by}) — {rec_result[:120]}")
            entry["recover"] = {
                "observed": f"{len(elements)} elements, {len(page_text.strip())}ch @ {page.url}",
                "pre_state": {
                    "url": pre_rec_url,
                    "fp": list(pre_rec_fp) if pre_rec_fp else None,
                },
                "reason": reason,
                "triaged_by": triaged_by,
                "strategy": strategy,
                "result": rec_result[:200],
            }
            _kv("jev-solver:recover:execute", strategy=strategy,
                reason=reason, by=triaged_by, result=rec_result[:120])
            recoveries.append({"step": steps, "reason": reason,
                               "strategy": strategy,
                               "result": rec_result[:200]})
            advance(machine, phase_trail, "recovered")  # recover -> see
            entry["recover"]["next"] = state_id(machine)
            # Only count recovery as an action when it actually
            # produced a non-error result — a failed recovery must
            # not reset the no-op counter or the no-effect rail.
            recovered = True
            _recovery_kind = {"refresh": "refresh", "back": "back",
                              "escape": "press_escape"}.get(strategy, "")
            if not rec_result.startswith("error") and _recovery_kind:
                acted = True
                last_effect_kind = _recovery_kind
                prev_acted = True
            else:
                # Recovery failed: treat like a noop so the next SEE
                # can detect whether the page is stuck.
                noops += 1
                _kv("jev-solver:recover:failed", strategy=strategy)

    # Last-resort synthesis: the per-step Noul never flags completion,
    # but the chat model can judge across pages. Once per run, when a
    # stop is imminent and 2+ distinct pages were read, ask it to call
    # the task from the notes. A refusal (or failure) falls through
    # to the honest stop below.
    if (not done and not stopped and not synth_attempted
            and not recovered
            and (dead_run >= dead_limit or noops >= noop_limit)
            and notes_url_count(notes) >= 2):
        synth_attempted = True
        _kv("jev-solver:synth:judge", status="started")
        try:
            verdict = await summarize_task(task=effective_task, notes=notes)
        except Exception as exc:  # noqa: BLE001
            verdict = None
            _kv("jev-solver:synth:judge", status="failed",
                error=exc.__class__.__name__)
        if verdict is not None and verdict.done:
            done = True
            advance(machine, phase_trail, "finish")  # verify -> done
            entry["act"] = f"DONE ({verdict.note[:80]})"
            history.append(f"step {steps}: DONE (synthesized) — {verdict.note[:80]}")
            _kv("jev-solver:gate:done", status="synthesized",
                note=verdict.note[:120])

    if not step_captcha:
        captcha_streak = 0
    if noops >= noop_limit and not done and not stopped and not recovered:
        stopped = True
        stop_reason = f"{noop_limit} consecutive no-ops"
        advance(machine, phase_trail, "abort")  # verify -> stopped
        _kv("jev-solver:run:stop",
            reason=f"{noop_limit} consecutive no-ops")
    if captcha_should_stop(captcha_streak) and not done and not stopped:
        stopped = True
        stop_reason = (f"image challenge persisted after "
                       f"{captcha_streak} attempts — needs a human")
        advance(machine, phase_trail, "abort")  # verify -> stopped
        _kv("jev-solver:run:stop", reason=stop_reason)
    if heal_abort is not None and not done and not stopped:
        stopped = True
        stop_reason = heal_abort
        advance(machine, phase_trail, "abort")  # verify -> stopped
        log(f"STOP   heal triage aborted — ending run honestly")
    # Step verdict in the contract taxonomy, then deferred reporting
    # so synth/recovery outcomes land in the artifacts.
    step_noop = not acted and not done
    entry["verdict"] = step_verdict(
        done=done, stopped=stopped, acted=acted,
        recovered=recovered,
        unresolved=(step_noop and len(recoveries) >= recover_cap))
    entry["phases"] = list(phase_trail)

    return {"done": done, "stopped": stopped, "stop_reason": stop_reason,
            "noops": noops, "dead_run": dead_run, "prev_fp": prev_fp,
            "prev_acted": prev_acted, "last_effect_kind": last_effect_kind,
            "synth_attempted": synth_attempted,
            "captcha_streak": captcha_streak, "recovered": recovered}


# ---------------------------------------------------------------------------
# Step record + post-loop summary
# ---------------------------------------------------------------------------


def _write_step_record(
    *,
    run_dir: str,
    steps: int,
    entry: dict,
    page,
    page_text: str,
    elements: list,
    focused,
    decision,
    start_url: str,
    effective_task: str,
    n_tabs: int,
    notes: list[str],
    visited: list[str],
    lessons,
    blocked,
    history: list[str],
    acted: bool,
    same_fp: bool,
    fp: tuple,
    report_mod,
    frontier=None,
) -> None:
    """Write the wire record, transcript append, and step JSONL."""
    from ..decide import build_questions
    from ..perception import build_state as _build_state

    frontier_summary: dict = {}
    if frontier is not None:
        try:
            frontier_summary = frontier.to_record()
        except Exception:
            frontier_summary = {}
        # Flush ledger events (discovered/visited/aliased) to the
        # canonical frontier.jsonl, then rewrite the snapshot.
        try:
            for ev in frontier.take_events():
                report_mod.write_frontier_event(
                    run_dir, {"n": steps, **ev})
            report_mod.write_frontier_snapshot(
                run_dir, frontier.snapshot())
        except Exception:
            pass

    state_packet = _build_state(
        task=effective_task, url=page.url, elements=elements,
        focused=focused, page_text=page_text, history=history,
        tabs=n_tabs, notes=notes, visited=visited, lessons=lessons,
        blocked=blocked, frontier=frontier_summary,
    )
    sites = [u for u in (start_url, page.url) if u]
    sites = list(dict.fromkeys(sites))
    report_mod.write_annotated_png(
        run_dir, steps, entry.get("_raw_png"), elements,
        chosen_idx=decision.element_idx, focused_frame=focused.frame or None,
    )
    report_mod.write_payload_jsonl(
        run_dir, steps, t=entry.get("t", 0.0), url=page.url,
        task=effective_task, state=state_packet,
        questions=build_questions(elements, sites, visited,
                                  frontier=frontier),
        answers=decision.raw,
        decision=(f"{decision.kind.value} conf={decision.confidence:.2f} "
                  f"item={decision.element_idx} "
                  f"ref={decision.item_ref} "
                  f"ready={decision.page_ready:.2f} text?={decision.needs_text:.2f} "
                  f"done?={decision.task_done:.2f} prog={decision.progress:.2f} "
                  f"appr={decision.approval:.2f}"),
        phases=list(entry.get("phases", [])),
    )
    report_mod.append_transcript(run_dir, entry)

    # CAPTCHA audit file: structured OCR analysis for this step, if any.
    # Written here (report_mod + run_dir are in scope) rather than in
    # the gate/act branch. No image bytes — analysis only.
    ocr_record = entry.get("captcha_ocr")
    if ocr_record:
        try:
            report_mod.write_captcha_json(run_dir, steps, {
                "n": steps, "url": page.url,
                "element_idx": entry.get("captcha_idx"),
                "ocr": ocr_record,
            })
        except Exception:  # noqa: BLE001
            pass

    # Shield audit file: structured native-solve analysis, if any.
    # Same contract as the CAPTCHA audit above (token lengths only).
    shield_record = entry.get("shield")
    if shield_record:
        try:
            report_mod.write_shield_json(run_dir, steps, {
                "n": steps, "url": page.url,
                "element_idx": entry.get("shield_idx"),
                "shield": shield_record,
            })
        except Exception:  # noqa: BLE001
            pass

    # Canonical machine-readable transcript: one structured JSONL
    # record per step — intent, proposed/executed calls, observable
    # change, error, recovery, replayability. Used for offline
    # analysis and step replay without raw DOM dumps.
    act_str = str(entry.get("act") or "")
    result_str = str(entry.get("result") or "")
    intent = act_str[:120] if act_str else ""
    err = result_str if result_str.startswith("error") else None
    # Derive the executed kind from the act_str pattern:
    #   "element #12 click"       -> click_item
    #   "element #12 challenge"   -> challenge
    #   "element #12 type=Nch"    -> type_at
    #   "type_at #12 (...)"       -> type_at (skipped / no-text)
    #   "key=Enter" / "key=Escape" -> press_enter / press_escape
    #   "refresh" / "back" / "close_others" -> as-is
    #   "goto <url>"               -> goto
    #   "wait" / "none" / "idle (...)" -> wait / none
    kind_for_exec = ""
    if act_str.startswith("element"):
        if " challenge" in act_str:
            kind_for_exec = "challenge"
        elif " type=" in act_str:
            kind_for_exec = "type_at"
        else:
            kind_for_exec = "click_item"
    elif act_str.startswith("type_at"):
        kind_for_exec = "type_at"
    elif act_str.startswith("key=Enter"):
        kind_for_exec = "press_enter"
    elif act_str.startswith("key=Escape"):
        kind_for_exec = "press_escape"
    elif act_str in ("refresh", "back", "close_others", "wait", "none"):
        kind_for_exec = act_str
    elif act_str.startswith("goto"):
        kind_for_exec = "goto"
    elif act_str.startswith("DONE"):
        kind_for_exec = "done"
    else:
        kind_for_exec = act_str.split()[0] if act_str else ""
    # Extract element idx from act_str for the executed ref/sel
    exec_idx = None
    m = re.search(r"#(\d+)", act_str)
    if m:
        exec_idx = int(m.group(1))
    executed_ref = None
    executed_sel = None
    if exec_idx is not None:
        e = next((x for x in elements if x.idx == exec_idx), None)
        if e is not None and e.ref:
            executed_ref = e.ref
            executed_sel = e.sel
    rec = entry.get("recover")
    recovery = None
    if rec and isinstance(rec, dict) and rec.get("strategy"):
        recovery = {
            "strategy": rec["strategy"],
            "result": rec.get("result", "")[:200],
            "reason": rec.get("reason", ""),
        }
    dec_text = str(entry.get("decide") or "")
    conf_match = re.search(r"conf=([\d.]+)", dec_text)
    confidence = float(conf_match.group(1)) if conf_match else 0.0
    replayable_kinds = {"goto", "click_item", "type_at", "challenge"}
    replayable = bool(
        not result_str.startswith("error")
        and (kind_for_exec in replayable_kinds
             or kind_for_exec == "refresh"
             or "goto" in act_str)
    )
    obs_change = bool(
        acted
        and not same_fp
        and page_settled(page_text)
    )
    report_mod.write_step_jsonl(run_dir, {
        "n": steps,
        "t": entry.get("t", 0.0),
        "url": page.url,
        "url_after": page.url,
        "intent": intent,
        "page_state": {
            "elements": len(elements),
            "text_len": len(page_text.strip()),
            "fingerprint": list(fp) if fp else None,
            "settled": page_settled(page_text),
        },
        "proposed": {
            "kind": "",
            "item": None,
            "url": None,
            "rationale": str(entry.get("propose") or "")[:200],
        },
        "decided": {
            "kind": kind_for_exec,
            "item": entry.get("decide"),
            "confidence": confidence,
            "nouls": entry.get("nouls", {}),
        },
        "executed": {
            "kind": kind_for_exec,
            "item": None,
            "ref": executed_ref,
            "selector": executed_sel,
            "url": None,
        },
        "action_type": act_str,
        "result": result_str[:300] if result_str else "",
        "observable_change": obs_change,
        "error": err,
        "recovery": recovery,
        "captcha_ocr": entry.get("captcha_ocr") or {},
        "shield": entry.get("shield") or {},
        "grid": entry.get("grid") or {},
        "twocaptcha": entry.get("twocaptcha") or {},
        "solver_rank": entry.get("solver_rank") or {},
        "frontier": frontier_summary,
        "confidence": confidence,
        "replayable": replayable,
        "verdict": entry.get("verdict", ""),
        "phases": entry.get("phases", []),
    })


async def _post_loop_summary(
    *,
    platform,
    run_dir: str,
    report_mod,
    start_url: str,
    task_text: str,
    steps: int,
    moves: int,
    done: bool,
    stopped: bool,
    stop_reason: str,
    recoveries: list,
    started: str,
    state: RunState,
    frontier=None,
    decided_model: str | None = None,
) -> dict:
    """Harvest cursor events, write run.json, log the summary."""
    from ..run.logging_utils import log

    cursor = await platform.harvest_cursor_events()
    cursor_path = report_mod.write_cursor(run_dir, cursor)
    summary = {
        "url": start_url, "task": task_text, "started": started,
        "finished": datetime.now().isoformat(timespec="seconds"),
        "steps": steps, "moves": moves, "task_done": done,
        "stopped": stopped, "stop_reason": stop_reason,
        "jev_model": decided_model,
        "recoveries": recoveries,
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
    frontier_state: dict = {}
    if frontier is not None:
        try:
            report_mod.write_frontier_snapshot(run_dir, frontier.snapshot())
            frontier_state = frontier.snapshot()
        except Exception:
            frontier_state = {}
    return {"state": state, "steps": steps, "moves": moves, "done": done,
            "stopped": stopped, "stop_reason": stop_reason,
            "cursor": cursor, "run_dir": run_dir,
            "frontier_state": frontier_state}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


async def run_decide_session(
    *,
    start_url: str,
    task: str = "",
    # Default OFF: Camoufox browser-level humanize degrades per dispatch
    # (measured 2026-09-18: 2.1s -> 8.4s -> timeouts within 3 moves at any
    # level, wedging every mouse op). Our own multi-hop loops still draw
    # human-like paths; pass --humanize to opt back into browser smoothing.
    humanize: bool | float = False,
    fps: float = 3.0,
    min_confidence: float = 0.4,
    budget_s: float = 120.0,
    max_steps: int = 50,
    headless: bool = False,
    steer_file: str = "steer.txt",
    seed_visited: list[str] | None = None,
    seed_frontier: dict | None = None,
) -> dict:
    from camoufox.async_api import AsyncCamoufox

    from ..browser import CamoufoxPlatform
    from ..run.agent_runner import read_steers
    from ..run.env_loader import load_env
    from .. import report as report_mod
    from .. import _log_sink
    from ..run.logging_utils import log

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
    decided_model: str | None = None  # versioned Jev id actually answering
    # (docs: a run logged against jev-latest still records which model
    # produced it — the alias moves, the record must not).
    paused = False
    noops = 0
    last_sig: tuple | None = None
    sig_run = 0
    mismatch_sig: tuple | None = None
    mismatch_run = 0
    escalated_sigs: set[tuple] = set()
    escalated_urls: set[str] = set()
    last_typed: tuple | None = None  # (element idx, page url) of the last successful type
    blank_streak = 0  # consecutive blank SEEs (dead loads draw waits forever)
    shot_streak = 0  # consecutive screenshot failures (wedged capture pipe)
    restart_tries = 0  # Jev-scored restart attempts this session
    recoveries: list[dict] = []  # audited recovery attempts (bounded, never blind repeats)
    recover_cap = max(3, max_steps // 100)
    recover_cap = max(3, max_steps // 100)
    # Stop rails scale with the step budget: a 3-no-op guillotine fits a
    # 50-step task, not a 1000-step mission. Long runs must survive
    # transient stalls; the rails still bound true fixation, proportionally.
    noop_limit, dead_limit, fix_limit = stop_limits(max_steps)
    # Long-horizon tracking (40-50 steps): visited-URL memory, extractive
    # notes that survive the 8-line history window, and dead-run detection
    # for actions with no observable effect. seed_visited carries coverage
    # across mission sessions so relaunches never re-read old pages.
    visited: list[str] = list(seed_visited or [])
    visited_set: set[str] = set(visited)
    # Crawl-frontier to-do ledger: every discovered link target is queued
    # here, every successful navigation marks it read (by URL *and* by
    # visible label, so opaque SERP redirect hrefs can't hide revisits).
    # Restored across mission sessions via seed_frontier snapshots.
    frontier = Frontier.from_snapshot(seed_frontier)
    frontier.seed_urls(seed_visited, step=0)
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
    # One declarative engine per session; every phase below is emitted
    # through advance(), so an illegal move raises instead of drifting.
    machine = RunMachine()
    # Heal bookkeeping, reset each step at the loop top:
    # heal_edge tracks an ACT->HEAL detour so VERIFY advances correctly.
    heal_edge: str | None = None
    # captcha_streak counts consecutive image-challenge dead-ends across
    # steps; step_captcha marks the current step so any other outcome
    # resets the streak (progress breaks the siege).
    captcha_streak = 0
    step_captcha = False
    steer_consumed = 0
    started = datetime.now().isoformat(timespec="seconds")
    t_end = None if budget_s <= 0 else time.time() + budget_s

    from ..capability.twocaptcha_client import launch_kwargs as _proxy_kw
    async with AsyncCamoufox(headless=headless, humanize=False,
                             **_proxy_kw()) as browser:
        # Browser-level humanize degrades per-dispatch at any level
        # (measured: 2.1s -> 8.4s -> timeouts within 3 moves). Our own
        # multi-hop loops draw human-like paths; pass --humanize to opt back
        # into browser smoothing (risky: may wedge mouse ops).
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
        _kv("capability:cursor:selftest",
            status="PASS" if await platform.cursor_selftest() else "WARN")
        known_tab_ids = platform.tab_ids()
        last_page_id = id(platform.page)

        while (t_end is None or time.time() < t_end) and steps < max_steps and not done and not stopped:
            if paused:
                log("PAUSED — human has the cursor; steer 'resume' to continue")
                await asyncio.sleep(5)
                continue
            steps += 1
            entry: dict = {"n": steps, "t": round(time.time(), 1)}
            _kv("jev-solver:step:start", step=steps, max_steps=max_steps)
            if not phase_trail:
                phase_trail.append(state_id(machine))  # == "see"
            elif state_id(machine) != "see":
                advance(machine, phase_trail, "continue_run")  # verify -> see
            heal_edge = None
            heal_abort = None
            heal_accounted = False
            step_captcha = False
            recovered = False
            # Per-step timing: answers "why is the run slow" with data —
            # propose (writer LLM), decide (Jev), act (dispatch + settle).
            t_top = time.time()
            t_proposed = t_top
            t_decided = t_top
            t_acted_at = t_top
            effective_task = task_text + (
                "\nNEW INSTRUCTIONS:\n" + "\n".join(instructions[-5:])
                if instructions else ""
            )

            # SEE
            entry["restart_attempts"] = restart_tries
            entry["_machine"] = machine
            entry["_phase_trail"] = phase_trail
            see = await _step_see(
                platform=platform, entry=entry, steps=steps,
                shot_streak=shot_streak, known_tab_ids=known_tab_ids,
                last_page_id=last_page_id, reading_until_step=reading_until_step,
                visited=visited, visited_set=visited_set, notes=notes,
                history=history, task_text=task_text,
                run_dir=run_dir, report_mod=report_mod, interval=interval,
                frontier=frontier,
            )
            if see.get("restarted") or see.get("skip_step"):
                shot_streak = see["shot_streak"]
                known_tab_ids = see["known_tab_ids"]
                if see.get("stopped"):
                    stopped = True
                    stop_reason = see["stop_reason"]
                restart_tries = entry.get("restart_attempts", restart_tries)
                # No transcript append / sleep here: _step_see already
                # accounted this step before returning.
                continue
            raw_png = see["raw_png"]
            elements = see["elements"]
            focused = see["focused"]
            page_text = see["page_text"]
            n_tabs = see["n_tabs"]
            blocked = see.get("blocked")
            shot_streak = see["shot_streak"]
            known_tab_ids = see["known_tab_ids"]
            last_page_id = see["last_page_id"]
            reading_until_step = see["reading_until_step"]

            state.url = page.url
            state.elements = elements
            state.focused = focused
            state.page_text = page_text
            state.history = list(history)
            entry["_raw_png"] = raw_png

            if is_blank_page(elements, page_text):
                blank_streak += 1
            else:
                blank_streak = 0

            # STEER
            steer = await _step_steer(
                platform=platform, entry=entry, steps=steps,
                moves=moves, steer_abs=steer_abs,
                steer_consumed=steer_consumed, history=history,
                instructions=instructions,
            )
            steer_consumed = steer["steer_consumed"]
            moves = steer["moves"]
            if steer["paused_delta"] is True:
                paused = True
            elif steer["paused_delta"] is False:
                paused = False
            if steer["stopped"]:
                stopped = True
                stop_reason = steer["stop_reason"]
                advance(machine, phase_trail, "abort")  # see -> stopped
                entry["phases"] = list(phase_trail)
                report_mod.append_transcript(run_dir, entry)
                break
            if paused:
                entry["phases"] = list(phase_trail)
                report_mod.append_transcript(run_dir, entry)
                await asyncio.sleep(interval)
                continue

            # Frontier seed from steer instructions: mission skip-lists
            # ("Name count URL" prose) and covered-URL lines mark targets
            # visited before either model can propose them. Idempotent —
            # re-seeding the same lines emits no new ledger events.
            if instructions:
                try:
                    from ..frontier import extract_seed_pairs, extract_urls
                    blob = "\n".join(instructions)
                    frontier.seed_urls(extract_urls(blob), step=steps)
                    frontier.seed_pairs(extract_seed_pairs(blob),
                                        step=steps)
                except Exception:
                    pass

            # PROPOSE + DECIDE
            pd = await _step_propose_decide(
                machine=machine, phase_trail=phase_trail,
                entry=entry, steps=steps,
                effective_task=effective_task, start_url=start_url,
                platform=platform, page=platform.page,
                elements=elements, focused=focused,
                page_text=page_text, history=history, n_tabs=n_tabs,
                notes=notes, visited=visited, lessons=lessons,
                blocked=blocked, reading_until_step=reading_until_step,
                mismatch_sig=mismatch_sig, mismatch_run=mismatch_run,
                escalated_sigs=escalated_sigs, escalated_urls=escalated_urls,
                last_typed=last_typed, blank_streak=blank_streak,
                run_dir=run_dir, report_mod=report_mod,
                frontier=frontier,
            )
            if pd.get("idle"):
                noops += 1
                entry["phases"] = list(phase_trail)
                report_mod.append_transcript(run_dir, entry)
                await asyncio.sleep(interval)
                continue
            decision = pd["decision"]
            try:
                seen_model = (getattr(decision, "raw", None) or {}).get(
                    "model")
                if seen_model:
                    decided_model = str(seen_model)
            except Exception:  # noqa: BLE001
                pass
            t_proposed = pd.get("t_proposed", t_top)
            t_decided = pd.get("t_decided", t_proposed)
            mismatch_sig = pd["mismatch_sig"]
            mismatch_run = pd["mismatch_run"]
            last_typed = pd["last_typed"]
            conf = pd["conf"]
            ready_now = pd["ready_now"]
            noops += pd.get("noop_bump", 0)

            # GATE + ACT (the big branching)
            entry["_reading_until_step"] = reading_until_step
            ga = await _step_gate_act(
                machine=machine, phase_trail=phase_trail,
                entry=entry, steps=steps,
                decision=decision, conf=conf,
                min_confidence=min_confidence, ready_now=ready_now,
                elements=elements, page=platform.page, platform=platform,
                noops=noops, moves=moves,
                last_sig=last_sig, sig_run=sig_run, fix_limit=fix_limit,
                last_typed=last_typed, last_effect_kind=last_effect_kind,
                blank_streak=blank_streak, page_text=page_text,
                history=history, effective_task=effective_task,
                start_url=start_url, run_dir=run_dir,
                recoveries=recoveries, recover_cap=recover_cap,
                notes=notes,
            )
            acted = ga["acted"]
            noops = ga["noops"]
            moves = ga["moves"]
            last_sig = ga["last_sig"]
            sig_run = ga["sig_run"]
            last_typed = ga["last_typed"]
            last_effect_kind = ga["last_effect_kind"]
            if ga.get("stopped"):
                stopped = True
                stop_reason = ga["stop_reason"]
            if ga.get("done"):
                done = True
            heal_edge = entry.get("heal_edge")
            heal_abort = entry.get("heal_abort")
            heal_accounted = entry.get("heal_accounted", False)
            step_captcha = entry.get("step_captcha", False)
            if step_captcha:
                captcha_streak = entry.get("captcha_streak", captcha_streak)

            # The action may have rebound the platform to a new tab
            # (click auto-adopt) — re-sync the local handle so the screenshot,
            # url, and artifacts below all read the CURRENT tab.
            page = platform.page
            t_acted_at = time.time()

            # Frontier visit accounting: every dispatched action lands the
            # loop somewhere — record the destination URL plus the acted
            # element's label, so the label→URL alias teaches the ledger
            # what opaque SERP hrefs actually point at. Re-visits bump the
            # counter (loop signal) instead of re-queueing.
            if acted:
                try:
                    mark_label = ""
                    if decision.element_idx is not None:
                        hit = next((e for e in elements
                                    if e.idx == decision.element_idx), None)
                        if hit is not None:
                            mark_label = hit.label or hit.text or ""
                    first_visit = frontier.mark_visited(
                        page.url, mark_label,
                        title=page_text[:80], step=steps)
                    entry["frontier_visit"] = {
                        "url": page.url, "label": mark_label[:80],
                        "first": bool(first_visit)}
                except Exception:
                    pass

            # REPORT artifacts for this step
            same_fp = (prev_fp is not None
                       and perception.page_fingerprint(page.url, page_text) == prev_fp)
            _write_step_record(
                run_dir=run_dir, steps=steps, entry=entry,
                page=page, page_text=page_text, elements=elements,
                focused=focused, decision=decision,
                start_url=start_url, effective_task=effective_task,
                n_tabs=n_tabs, notes=notes, visited=visited,
                lessons=lessons, blocked=blocked, history=history,
                acted=acted,
                same_fp=same_fp,
                fp=perception.page_fingerprint(page.url, page_text),
                report_mod=report_mod,
                frontier=frontier,
            )
            _kv("jev-solver:timing:step",
                propose=f"{t_proposed - t_top:.1f}s",
                decide=f"{t_decided - t_proposed:.1f}s",
                act=f"{t_acted_at - t_decided:.1f}s")

            # Deferred reporting note: entry["phases"], the wire record,
            # and the transcript append live AFTER the stops below, so
            # synth verdicts, recovery audits, and final phases land in the
            # artifacts instead of only in history.

            # VERIFY + RECOVER
            vr = await _step_verify_recover(
                machine=machine, phase_trail=phase_trail,
                entry=entry, steps=steps, page=page,
                page_text=page_text, elements=elements,
                blocked=blocked, acted=acted,
                done=done, stopped=stopped, stop_reason=stop_reason,
                noops=noops, noop_limit=noop_limit,
                dead_run=dead_run, dead_limit=dead_limit,
                prev_fp=prev_fp, prev_acted=prev_acted,
                last_effect_kind=last_effect_kind,
                recoveries=recoveries, recover_cap=recover_cap,
                same_fp=same_fp, synth_attempted=synth_attempted,
                captcha_streak=captcha_streak, step_captcha=step_captcha,
                heal_edge=heal_edge, heal_abort=heal_abort,
                notes=notes, effective_task=effective_task,
                run_dir=run_dir, report_mod=report_mod,
                history=history, platform=platform,
            )
            done = vr["done"]
            stopped = vr["stopped"]
            stop_reason = vr["stop_reason"]
            noops = vr["noops"]
            # Dead-action ledger: the no-effect rail just proved THIS
            # step's action changed nothing readable. Record it against
            # the acted element so both models route around the target
            # after the threshold — repeating a dead toggle with no new
            # information is never progress (seen live: /sorry/ checkbox
            # toggled 6× while the writer begged to go to Bing).
            if vr["dead_run"] > dead_run and decision.element_idx is not None:
                try:
                    hit = next((e for e in elements
                                if e.idx == decision.element_idx), None)
                    if hit is not None:
                        n = frontier.mark_dead(
                            decision.kind.value,
                            hit.label or hit.text or "",
                            perception.host_of(hit.href), step=steps)
                        if n >= frontier.DEAD_THRESHOLD:
                            log(f"DEAD {decision.kind.value} "
                                f"#{decision.element_idx} x{n} — routing around")
                            history.append(
                                f"step {steps}: {decision.kind.value} "
                                f"#{decision.element_idx} dead x{n} "
                                f"(no observable effect) — route around it")
                except Exception:
                    pass
            dead_run = vr["dead_run"]
            prev_fp = vr["prev_fp"]
            prev_acted = vr["prev_acted"]
            last_effect_kind = vr["last_effect_kind"]
            synth_attempted = vr["synth_attempted"]
            captcha_streak = vr["captcha_streak"]
            recovered = vr["recovered"]

            report_mod.write_wire(run_dir, {
                "n": steps, "t": entry.get("t"), "url": page.url, "tabs": n_tabs,
                "see": entry.get("see"), "decide": entry.get("decide"),
                "nouls": entry.get("nouls"), "progress": entry.get("progress"),
                "act": entry.get("act"), "result": entry.get("result"),
                "verdict": entry.get("verdict"), "recover": entry.get("recover"),
                "captcha_ocr": entry.get("captcha_ocr"),
                "shield": entry.get("shield"),
                "frontier": frontier.to_record(),
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
                     "ref": e.ref, "aria": e.aria,
                     "cx": e.cx, "cy": e.cy}
                    for e in elements
                ],
                "notes": notes, "visited": visited,
            })
            report_mod.append_transcript(run_dir, entry)
            await asyncio.sleep(interval)

    return await _post_loop_summary(
        platform=platform, run_dir=run_dir, report_mod=report_mod,
        start_url=start_url, task_text=task_text,
        steps=steps, moves=moves, done=done, stopped=stopped,
        stop_reason=stop_reason, recoveries=recoveries,
        started=started, state=state, frontier=frontier,
        decided_model=decided_model,
    )
