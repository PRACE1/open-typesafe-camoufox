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
from datetime import datetime

from . import perception
from .actions import click_item, goto_url, press_key, type_at
from .capability.human_move import HUMANIZE_LEVEL
from .decide import Kind, decide_action
from .deps import RunState
from .perception import is_credential_element
from .writer import compose_text, propose_url

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
    max_steps: int = 40,
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

        while time.time() < t_end and steps < max_steps and not done and not stopped:
            if paused:
                log("PAUSED — human has the cursor; steer 'resume' to continue")
                await asyncio.sleep(5)
                continue
            steps += 1
            entry: dict = {"n": steps, "t": round(time.time(), 1)}
            log(f"──── step {steps}/{max_steps} " + "─" * 40)
            effective_task = task_text + (
                "\nNEW INSTRUCTIONS:\n" + "\n".join(instructions[-5:])
                if instructions else ""
            )

            # SEE
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
            state.url = page.url
            state.elements = elements
            state.focused = focused
            state.page_text = page_text
            state.history = list(history)
            report_mod.write_raw_png(run_dir, steps, raw_png)
            see_line = (
                f"{len(elements)} elements · tabs={n_tabs} · focused={focused.role or '-'}"
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
                report_mod.append_transcript(run_dir, entry)
                if stopped:
                    break
                await asyncio.sleep(interval)
                continue

            # DECIDE
            try:
                decision = await decide_action(
                    task=effective_task, url=page.url, start_url=start_url,
                    elements=elements, focused=focused, page_text=page_text,
                    history=history, tabs=n_tabs,
                )
            except Exception as exc:
                log(f"DECIDE failed ({exc.__class__.__name__}); idling this step")
                entry["decide"] = f"failed: {exc}"
                history.append(f"step {steps}: decide failed, idled")
                noops += 1
                report_mod.append_transcript(run_dir, entry)
                await asyncio.sleep(interval)
                continue
            conf = decision.confidence
            log(f"DECIDE {decision.kind.value} conf={conf:.2f}"
                + (f" item=#{decision.element_idx}" if decision.element_idx is not None else "")
                + (f" site={decision.target_url or 'other...'}" if decision.kind == Kind.GOTO else ""))
            entry["decide"] = (
                f"{decision.kind.value} conf={conf:.2f} "
                f"item={decision.element_idx} site={decision.target_url or ('other' if decision.propose_url else None)}"
            )
            entry["think"] = entry["decide"]
            report_mod.write_answers_json(run_dir, steps, decision.raw)

            if conf < min_confidence and decision.kind not in (Kind.DONE,):
                log(f"GATE   conf {conf:.2f} < {min_confidence} — idle (no-op {noops + 1})")
                history.append(f"step {steps}: low conf {conf:.2f}, idled")
                entry["act"] = "idle (low confidence)"
                noops += 1
            # Loop-guard: the tab, clear, and click systems all report into
            # the decision, but a confident classifier can still fixate
            # (same click 5x). Three identical click/type targets in a row
            # force a wait here instead of executing again.
            guard_trip = False
            if decision.kind in (Kind.CLICK_ITEM, Kind.TYPE_AT) and decision.element_idx is not None:
                guard_trip, sig_run = loop_guard_trip(
                    last_sig, sig_run,
                    (decision.kind.value, decision.element_idx, page.url),
                )
                last_sig = (decision.kind.value, decision.element_idx, page.url)
            if guard_trip:
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
                if not page_settled(page_text):
                    msg = (f"step {steps}: DONE REJECTED (page not settled) — "
                           "wait one step and re-observe")
                    log(f"DONE denied: {msg}")
                    history.append(msg)
                    entry["act"] = "DONE rejected (page not settled)"
                elif not page_text.strip():
                    msg = f"step {steps}: DONE REJECTED (empty page text)"
                    log(f"DONE denied: {msg}")
                    history.append(msg)
                    entry["act"] = "DONE rejected (empty page)"
                else:
                    note = page_text.strip()[:200]
                    done = True
                    entry["act"] = f"DONE ({note[:80]})"
                    history.append(f"step {steps}: DONE — {note[:80]}")
                    log(f"DONE   {note[:120]}")
            elif decision.kind == Kind.GOTO:
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
                    entry["act"] = f"goto {target}"
                    entry["result"] = res
                    history.append(f"step {steps}: goto {target} — {res}")
                    moves += 1
                    noops = 0
            elif decision.kind == Kind.PRESS_ENTER:
                log("ACT    key=Enter")
                res = await press_key(platform, "Enter")
                log(f"RESULT {res}")
                entry["act"] = "key=Enter"
                entry["result"] = res
                history.append(f"step {steps}: pressed Enter — {res}")
                moves += 1
                noops = 0
            elif decision.kind == Kind.REFRESH:
                log("ACT    refresh")
                res = await platform.refresh_page()
                log(f"RESULT {res}")
                entry["act"] = "refresh"
                entry["result"] = res
                history.append(f"step {steps}: refreshed — {res}")
                moves += 1
                noops = 0
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
            else:  # CLICK_ITEM / TYPE_AT
                idx = decision.element_idx
                by_idx = {e.idx: e for e in elements}
                if idx is None or idx not in by_idx:
                    log(f"ACT    {decision.kind.value} without valid item — idle")
                    history.append(f"step {steps}: {decision.kind.value} without item, idled")
                    entry["act"] = f"{decision.kind.value} (no item)"
                    noops += 1
                elif decision.kind == Kind.CLICK_ITEM:
                    log(f"ACT    element #{idx} (scroll+circle+click)")
                    res = await click_item(platform, elements, idx)
                    log(f"RESULT {res}")
                    entry["act"] = f"element #{idx} click"
                    entry["result"] = res
                    history.append(f"step {steps}: clicked element #{idx} — {res}")
                    moves += 1
                    noops = 0
                else:  # TYPE_AT
                    elem = by_idx[idx]
                    label = elem.label or elem.placeholder or elem.text or elem.id or f"element #{idx}"
                    cred = is_credential_element(elem)
                    log(f"ACT    element #{idx} type (credential={cred})")
                    composed = await compose_text(
                        task=effective_task, field_label=label,
                        placeholder=elem.placeholder, nearby_text=page_text,
                        history=history, is_credential=cred,
                    )
                    text_to_type: str | None = None
                    if composed.fill and composed.text.strip():
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
                        history.append(f"step {steps}: writer declined to fill #{idx}")
                        entry["act"] = f"type_at #{idx} (writer declined)"
                        noops += 1
                    if text_to_type is not None:
                        res = await type_at(platform, elements, idx, text_to_type)
                        log(f"RESULT {res}")
                        entry["act"] = f"element #{idx} type={len(text_to_type)}ch"
                        entry["result"] = res
                        history.append(f"step {steps}: typed at #{idx} — {res}")
                        moves += 1
                        noops = 0

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
                tabs=n_tabs,
            )
            sites = [u for u in (start_url, page.url) if u]
            sites = list(dict.fromkeys(sites))
            report_mod.write_annotated_png(
                run_dir, steps, raw_png, elements,
                chosen_idx=decision.element_idx, focused_frame=focused.frame or None,
            )
            report_mod.write_payload_txt(
                run_dir, steps, state_packet, build_questions(elements, sites),
                f"{decision.kind.value} conf={decision.confidence:.2f} "
                f"item={decision.element_idx}",
            )
            report_mod.append_transcript(run_dir, entry)

            if noops >= 2 and not done:
                stopped = True
                stop_reason = "two consecutive no-ops"
                log("STOP   two consecutive no-ops — ending run")
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
        report_mod.append_transcript(run_dir, {"final": summary})

    log("=" * 60)
    log(f"SUMMARY steps={steps} moves={moves} done={done} stopped={stopped} {stop_reason}")
    log(f"CURSOR {len(cursor['moves'])} moves + {len(cursor['clicks'])} clicks -> {cursor_path}")
    log(f"TRANSCRIPT {os.path.join(run_dir, 'transcript.jsonl')}")
    log("=" * 60)
    return {"state": state, "steps": steps, "moves": moves, "done": done,
            "stopped": stopped, "stop_reason": stop_reason,
            "cursor": cursor, "run_dir": run_dir}
