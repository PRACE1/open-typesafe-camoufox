"""
Task-driven run loop for open-typesafe-camoufox — watchable.

Every step prints a block:
    ──── step N/max ────
    SEE    braille 40x12 · Jev x=0.531 y=0.719 conf=0.82
    THINK  planner's one-sentence reasoning (streamed)
    ACT    move(0.531,0.719) + click
    RESULT moved to (0.531,0.719) + click (0.8s)

Watchdog: planner timeout -> PLANNER LATE + idle cursor swipe (no dead air).
Jev failure/blank frame -> SEE no-hint (reason); planner continues from
history alone.

Steering: --steer file (default steer.txt) polled each step.
    stop        -> clean shutdown
    goto <url>  -> mid-run navigation (cursor buffer survived via safe_goto)
    anything    -> NEW INSTRUCTION appended for all later steps

Persistent log: runs/<timestamp>/transcript.jsonl (one JSON line per step)
+ cursor.json (video-agent-compatible cursor trail).

Capture stays at 60fps in the browser; Jev + planner inference is decimated
to 2-5fps keyframes. HEADED by default: watch the Camoufox window, hands off
mouse/keyboard. Secrets ({ENV_NAME} placeholders) resolve inside move_cursor
only — prompts and logs carry placeholders, values are masked by length.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime

from src.capability import CamoufoxDeps
from src.capability.human_move import HUMANIZE_LEVEL
from src.run.logging_utils import log

PLANNER_TIMEOUT_S = 90.0

_READ_OK_RE = re.compile(
    r"frame \((\d+)x(\d+) braille\) -> jev x=([\d.]+) y=([\d.]+) click=(\d) conf=([\d.]+)"
)


def parse_steer(line: str) -> tuple[str, str | None] | None:
    """Parse one steer-file line -> (kind, payload).

    kinds: 'stop' | 'pause' | 'resume' | 'goto' | 'instruction'. None for blank lines.
    """
    line = line.strip()
    if not line:
        return None
    low = line.lower()
    if low == "stop":
        return ("stop", None)
    if low == "pause":
        return ("pause", None)
    if low == "resume":
        return ("resume", None)
    if low.startswith("goto "):
        target = line[5:].strip()
        if target:
            return ("goto", target)
    return ("instruction", line)


def _handle_done(step, steps: int) -> tuple[bool, str | None]:
    """Resolve step.done -> (accepted, reject_reason).

    'done' is only accepted when the note spells out the concrete outcome;
    an empty note means the planner finished before actually reading the page
    (e.g. marked done while the results page was still loading). The
    reject_reason is logged to the feed and appended to history so the next
    turn carries the correction.
    """
    if not step.done:
        return False, None
    if not (step.note or "").strip():
        return False, (
            f"step {steps}: DONE REJECTED (empty note) — the note must quote the "
            "concrete outcome from PAGE TEXT (fact / confirmation words), then repeat done"
        )
    return True, None


def read_steers(path: str, consumed: int) -> tuple[list[tuple[str, str | None]], int]:
    """Read steer lines newly appended since `consumed`. Returns (list, new_count)."""
    if not path or not os.path.exists(path):
        return ([], consumed)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ([], consumed)
    out = []
    for ln in lines[consumed:]:
        parsed = parse_steer(ln)
        if parsed:
            out.append(parsed)
    return out, len(lines)


def _make_run_dir() -> str:
    root = os.path.abspath(os.path.join(os.getcwd(), "runs"))
    os.makedirs(root, exist_ok=True)
    d = os.path.join(root, datetime.now().strftime("%Y%m%d-%H%M%S"))
    os.makedirs(d, exist_ok=True)
    return d


def _write_transcript(path: str, entry: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def _idle_swipe(capability, deps: CamoufoxDeps, ctx) -> str:
    """Small humanized move near the last Jev hint — keeps the window alive
    while the planner is late."""
    from jev_tools import MoveCursorAction

    base_x, base_y = 0.5, 0.45
    hint = deps.browser_urls.get("__jev_last__", "")
    if hint.count(",") == 2:
        try:
            base_x, base_y = (float(v) for v in hint.split(",")[:2])
        except ValueError:
            pass
    rng = random.Random()
    x = max(0.05, min(0.95, base_x + rng.uniform(-0.03, 0.03)))
    y = max(0.05, min(0.95, base_y + rng.uniform(-0.03, 0.03)))
    res = await capability.move_cursor(ctx, MoveCursorAction(x=x, y=y))
    return f"idle swipe -> ({x:.3f},{y:.3f}): {res}"


def _format_elements(elements: list[dict]) -> str:
    """Render the element map for the planner prompt."""
    if not elements:
        return "(no actionable elements found on this page)"
    lines = []
    for e in elements:
        label = e.get("label") or e.get("placeholder") or e.get("text") or e.get("id") or "?"
        extra = f" ({e['type']})" if e.get("type") else ""
        lines.append(
            f"[{e['idx']}] {e['kind']}{extra} \"{label}\" @ {e['cx']:.3f},{e['cy']:.3f}"
        )
    return "\n".join(lines)


async def run_jev_session(
    *,
    start_url: str,
    task: str = "",
    humanize: bool | float = HUMANIZE_LEVEL,
    fps: float = 3.0,
    confidence: float = 0.7,
    budget_s: float = 120.0,
    max_steps: int = 40,
    headless: bool = False,
    steer_file: str = "steer.txt",
) -> dict:
    from camoufox.async_api import AsyncCamoufox

    from jev_agent import build_step_agent
    from jev_tools import MoveCursorAction, ReadFrameAction
    from src.capability.jev_actions import JevCapability
    from src.run.env_loader import load_env

    load_env()
    fps = max(2.0, min(5.0, fps))
    interval = 1.0 / fps
    task_text = task.strip() or f"Explore {start_url} and report what you see."
    steer_abs = os.path.abspath(steer_file)
    run_dir = _make_run_dir()
    transcript_path = os.path.join(run_dir, "transcript.jsonl")

    from src import _log_sink

    _log_sink.set_file(os.path.join(run_dir, "run.log"))

    log("=" * 60)
    log("open-typesafe-camoufox — WATCH THE BROWSER WINDOW (headed) · hands off mouse/keyboard")
    log(f"URL    {start_url}")
    log(f"TASK   {task_text}")
    log(f"MODE   headed={not headless} humanize={humanize} fps={fps}")
    log(f"BUDGET {budget_s:.0f}s / {max_steps} steps")
    log(f"STEER  {steer_abs}  (lines: 'stop' | 'goto <url>' | 'pause' | 'resume' | any instruction)")
    log(f"LOG    {run_dir}  (run.log = full feed incl. diagnostics)")
    log("=" * 60)

    stepper = build_step_agent()
    capability = JevCapability()
    deps = CamoufoxDeps(browser_urls={"start": start_url}, log=[task_text])
    ctx = type("Ctx", (), {"deps": deps})()

    steps = 0
    moves = 0
    done = False
    stopped = False
    paused = False
    history: list[str] = []
    instructions: list[str] = []
    steer_consumed = 0
    transcript: dict = {
        "url": start_url,
        "task": task_text,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    t_end = time.time() + budget_s
    result_extra: dict = {}

    from ..capability.twocaptcha_client import launch_kwargs as _proxy_kw
    async with AsyncCamoufox(headless=headless, humanize=humanize,
                             **_proxy_kw()) as browser:
        page = await browser.new_page()
        await capability.set_page(page)
        try:
            await asyncio.wait_for(capability.safe_goto(start_url), timeout=60.0)
        except Exception as exc:
            log(f"initial goto failed ({exc}), retrying plain goto")
            await page.goto(start_url, wait_until="load")
            capability._last_url = page.url
            await capability.reinject_tracker()
        await capability.start_cursor_tracking()
        log(f"tracker selftest: {'PASS' if await capability.cursor_selftest() else 'WARN — cursor.json may be incomplete'}")

        while time.time() < t_end and steps < max_steps and not done and not stopped:
            if paused:
                log("PAUSED — human has the cursor; steer 'resume' to continue")
                await asyncio.sleep(5)
                continue
            steps += 1
            t0 = time.time()
            entry: dict = {"n": steps, "t": round(time.time(), 1)}
            log(f"──── step {steps}/{max_steps} " + "─" * 40)

            # SEE: braille frame + Jev grounding hint
            read_msg = await capability.read_frame(ctx, ReadFrameAction(fps=fps))  # type: ignore[arg-type]
            m = _READ_OK_RE.search(read_msg)
            if m:
                see_line = f"braille {m.group(1)}x{m.group(2)} · Jev x={m.group(3)} y={m.group(4)} conf={m.group(6)} click={m.group(5)}"
            else:
                see_line = f"no-hint ({read_msg})"
            log(f"SEE    {see_line}")
            entry["see"] = see_line
            hint = deps.browser_urls.get("__jev_last__", "none") if m else "none"
            entry["hint"] = hint

            # PAGE TEXT: visible body text — ground truth the planner reads
            # (braille alone can't read low-contrast / mostly-white pages).
            page_text = await capability.get_page_text()
            entry["page_text"] = page_text[:400]
            if page_text.strip():
                log(f"TEXT   {page_text.strip()[:160]}")

            # STEER: poll the steer file for new lines
            new_steers, steer_consumed = read_steers(steer_abs, steer_consumed)
            for kind, payload in new_steers:
                label = payload if kind == "goto" else (payload or "stop")
                log(f"STEER  {label}")
                entry.setdefault("steer", []).append(label)
                if kind == "stop":
                    stopped = True
                    break
                if kind == "pause":
                    paused = True
                    log("STEER  PAUSED — human has the mouse/keyboard ('resume' to continue)")
                elif kind == "resume":
                    paused = False
                    log("STEER  RESUMED — agent back in control")
                if kind == "goto":
                    try:
                        await capability.safe_goto(payload)
                        log(f"STEER  landed on {capability._last_url}")
                    except Exception as exc:
                        log(f"STEER  goto failed: {exc}")
                elif kind == "instruction" and payload:
                    instructions.append(payload)
            if stopped:
                break

            # PROBE: fresh element map for this page (video-agent targeting)
            elements = await capability.find_elements()
            entry["elements_n"] = len(elements)
            entry["elements"] = _format_elements(elements)  # full map every step in the transcript
            log(f"MAP    {len(elements)} actionable: " + (
                "; ".join(f"[{e['idx']}]{e['kind']}" for e in elements[:8]) + (" …" if len(elements) > 8 else "")
            ))

            # THINK + ACT: one planner call per step
            frame = deps.browser_urls.get("__jev_frame__", "")
            grid = deps.browser_urls.get("__jev_grid__", "?")
            hist = "\n".join(history[-8:]) or "(no actions yet)"
            prompt = (
                f"TASK: {task_text}\n"
                + (
                    "NEW INSTRUCTIONS (override the task where they conflict):\n"
                    + "\n".join(instructions[-5:])
                    + "\n"
                    if instructions
                    else ""
                )
                + f"ELEMENT MAP (visible, reading order; target by idx):\n{_format_elements(elements)}\n"
                + f"Jev grounding hint (x,y,conf): {hint}\n"
                + f"PAGE TEXT (visible words on the page):\n{page_text.strip() or '(no visible text)'}\n"
                + f"Page braille ({grid} chars):\n{frame}\n"
                + f"HISTORY:\n{hist}\n"
                + "Return the single next JevStep."
            )
            t_plan = time.time()
            try:
                res = await asyncio.wait_for(stepper.run(prompt), timeout=PLANNER_TIMEOUT_S)
                step = res.output
            except Exception as exc:
                log(f"WAIT   planner late/failed after {time.time() - t_plan:.0f}s ({exc.__class__.__name__})")
                swipe = await _idle_swipe(capability, deps, ctx)
                log(f"ACT    {swipe}")
                entry["act"] = swipe
                entry["think"] = "(planner late)"
                history.append(f"step {steps}: planner late, idled")
                _write_transcript(transcript_path, entry)
                await asyncio.sleep(interval)
                continue

            thinking = (step.reasoning or "").strip() or (step.note or "(no reasoning given)")
            if step.goto:
                log(f"THINK  {thinking}")
                log(f"NAV    -> {step.goto}")
                try:
                    await capability.safe_goto(step.goto)
                    nav_msg = f"navigated to {capability._last_url}"
                except Exception as exc:
                    nav_msg = f"nav failed: {exc}"
                log(f"RESULT {nav_msg}")
                entry["act"] = f"goto {step.goto}"
                entry["result"] = nav_msg
                entry["think"] = thinking
                history.append(f"step {steps}: goto {step.goto} — {nav_msg}")
                done, reject = _handle_done(step, steps)
                if reject:
                    log(f"DONE denied: {reject}")
                    history.append(reject)
                _write_transcript(transcript_path, entry)
                await asyncio.sleep(interval)
                continue
            if step.element is not None:
                act_desc = [f"element #{step.element} (scroll+circle+click)"]
            else:
                act_desc = [f"move({step.x:.3f},{step.y:.3f})"]
            if step.click:
                act_desc.append("click")
            if step.type_text is not None:
                act_desc.append(f"type={len(step.type_text)}ch")
            if step.key:
                act_desc.append(f"key={step.key}")
            if step.done:
                act_desc.append("DONE")
            think_only = step.done and not (step.click or step.type_text or step.key) and step.element is None
            log(f"THINK  {thinking}")
            if not think_only:
                log(f"ACT    {' + '.join(act_desc)}")
                t_act = time.time()
                action = MoveCursorAction(
                    x=step.x, y=step.y, click=step.click, humanize=True,
                    element=step.element,
                    type_text=step.type_text, key=step.key,  # type: ignore[arg-type]
                )
                act_res = await capability.move_cursor(ctx, action)  # type: ignore[arg-type]
                moves += 1
                log(f"RESULT {act_res} ({time.time() - t_act:.1f}s)")
                entry["act"] = " + ".join(act_desc)
                entry["result"] = act_res
            else:
                entry["act"] = f"DONE ({thinking[:80]})"
            entry["think"] = thinking
            history.append(f"step {steps}: {thinking} [{(' + '.join(act_desc))}]")
            _write_transcript(transcript_path, entry)
            done, reject = _handle_done(step, steps)
            if reject:
                log(f"DONE denied: {reject}")
                history.append(reject)
            await asyncio.sleep(interval)

        # Final harvest -> cursor.json (video-agent compatible) + transcript footer
        cursor = await capability.harvest_cursor_events()
        cursor_path = os.path.join(run_dir, "cursor.json")
        with open(cursor_path, "w", encoding="utf-8") as f:
            json.dump(cursor, f)
        transcript.update(
            {
                "finished": datetime.now().isoformat(timespec="seconds"),
                "steps": steps,
                "moves": moves,
                "task_done": done,
                "stopped_by_steer": stopped,
                "cursor_moves": len(cursor["moves"]),
                "cursor_clicks": len(cursor["clicks"]),
                "run_dir": run_dir,
            }
        )
        _write_transcript(transcript_path, {"final": transcript})

    log("=" * 60)
    log(f"SUMMARY steps={steps} moves={moves} done={done} stopped={stopped}")
    log(f"CURSOR {len(cursor['moves'])} moves + {len(cursor['clicks'])} clicks -> {cursor_path}")
    log(f"TRANSCRIPT {transcript_path}")
    log("=" * 60)
    return {"deps": deps, "steps": steps, "moves": moves, "done": done, "stopped": stopped, "cursor": cursor, "run_dir": run_dir, **result_extra}