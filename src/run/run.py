"""
CLI entry for open-typesafe-camoufox. Thin argparse wrapper over runner.run_decide_session.
Ported from video-agent/src/run/run.py (Cap/Twenty/R2 stripped).

The browser is HEADED by default so you can watch the run. Hands off the
mouse, keyboard, and window focus while it runs. Each step prints a
SEE/THINK/ACT/RESULT block; a steer file lets you redirect mid-run.

Usage:
  # prove the window opens + all deps resolve (no task, no creds needed):
  uv run python -m src.run.run --url https://example.com --preflight

  # real watchable run:
  uv run python -m src.run.run --url https://x.com/login --task "Sign in with {TWITTER_USERNAME} / {TWITTER_PASSWORD}" --budget 120 --max-steps 40
  # while it runs, steer from another terminal:
  Add-Content steer.txt "skip the phone prompt"
  Add-Content steer.txt "goto https://x.com/home"
  Add-Content steer.txt "stop"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SRC = os.path.abspath(os.path.join(_HERE, ".."))
for _p in (_ROOT, _SRC):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from src.run.logging_utils import log, set_verbose  # noqa: E402
from src.run.env_loader import load_env  # noqa: E402
from src.run.agent_runner import run_jev_session  # noqa: E402
from src.runner import run_decide_session  # noqa: E402
from src.capability.human_move import HUMANIZE_LEVEL  # noqa: E402


def _check_placeholders(task: str) -> list[str]:
    """Env vars referenced by the task via {NAME} that are missing/empty."""
    names = re.findall(r"\{([A-Z_][A-Z0-9_]*)\}", task)
    return sorted({n for n in names if not os.environ.get(n)})


async def _preflight(url: str, humanize: bool | float) -> int:
    """Prove everything resolves and the HEADED window actually opens.

    1) API keys present (typesafe tolerated — Jev has an offline fallback)
    2) task placeholders resolvable (no invented secrets)
    3) Camoufox binary resolvable + headed launch + new page within 90s
    Fails loud with the exact missing piece; never hangs silently.
    """
    missing_keys = [k for k in ("GROQ_API_KEY", "TYPESAFE_API_KEY") if not os.environ.get(k)]
    if "GROQ_API_KEY" in missing_keys:
        log("PREFLIGHT FAIL: GROQ_API_KEY missing (.env.local)")
        return 4
    if "TYPESAFE_API_KEY" in missing_keys:
        log("PREFLIGHT WARN: TYPESAFE_API_KEY missing — Jev falls back to a heuristic (no real grounding)")

    from camoufox.async_api import AsyncCamoufox

    log("PREFLIGHT: launching headed Camoufox (watch for the window)...")
    try:
        async with asyncio.timeout(90):
            async with AsyncCamoufox(headless=False, humanize=humanize) as browser:
                page = await browser.new_page()
                await page.goto("about:blank")
                vp = page.viewport_size or {}
                log(
                    f"PREFLIGHT OK: browser up, viewport {vp.get('width')}x{vp.get('height')}, "
                    f"url={page.url}"
                )
        return 0
    except TimeoutError:
        log("PREFLIGHT FAIL: browser launch took >90s (Camoufox version check stalling?)")
        return 3
    except Exception as exc:
        log(f"PREFLIGHT FAIL: {exc.__class__.__name__}: {exc}")
        return 3


def _covered_urls(run_dir: str, known: list[str]) -> list[str]:
    """Merge a session's wire.jsonl URLs into the mission coverage list.

    Pure accumulation, order-stable, deduped. Never raises (a missing or
    corrupt wire file just contributes nothing).
    """
    urls = list(known)
    seen = set(urls)
    try:
        with open(os.path.join(run_dir, "wire.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    url = json.loads(line).get("url", "")
                except (ValueError, AttributeError):
                    continue
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
    except OSError:
        pass
    return urls


MISSION_MAX_SESSIONS = 10


async def _mission(start_url: str, task: str, steer_file: str, fps: float,
                   min_confidence: float, budget_s: float, max_steps: int,
                   headless: bool, humanize: bool | float = False) -> dict:
    """Resume-until-done driver: stopped sessions relaunch with coverage.

    Each session shares the total step budget and wall-clock deadline;
    covered URLs seed the next session's visited memory plus a steer line,
    so a mission cannot die on a transient stall — only done, exhaustion,
    an empty (0-step) session, or the session cap ends it.
    """
    deadline = None if budget_s <= 0 else time.time() + budget_s
    total_steps = 0
    total_moves = 0
    covered: list[str] = []
    seeded: list[str] = []
    done = False
    session = 0
    last_run_dir = ""
    while True:
        session += 1
        if session > MISSION_MAX_SESSIONS:
            log(f"MISSION session cap ({MISSION_MAX_SESSIONS}) — ending mission")
            break
        remaining_steps = max_steps - total_steps
        if remaining_steps <= 0:
            log("MISSION step budget exhausted — ending mission")
            break
        if deadline is not None:
            remaining = deadline - time.time()
            if remaining <= 0:
                log("MISSION wall-clock budget exhausted — ending mission")
                break
        else:
            remaining = 0
        fresh = [u for u in covered if u not in seeded]
        if fresh:
            seeded.extend(fresh)
            try:
                with open(steer_file, "a", encoding="utf-8") as f:
                    f.write(f"instruction SESSION {session}: already covered, do not revisit: "
                            + " | ".join(fresh[:50]) + "\n")
            except OSError as exc:
                log(f"MISSION steer append failed ({exc}) — continuing unseeded")
        log(f"MISSION session {session}/{MISSION_MAX_SESSIONS} "
            f"(steps used {total_steps}/{max_steps})")
        result = await run_decide_session(
            start_url=start_url,
            task=task,
            fps=fps,
            min_confidence=min_confidence,
            budget_s=remaining,
            max_steps=remaining_steps,
            headless=headless,
            humanize=humanize,
            steer_file=steer_file,
            seed_visited=covered,
        )
        total_steps += result["steps"]
        total_moves += result["moves"]
        last_run_dir = result["run_dir"]
        covered = _covered_urls(result["run_dir"], covered)
        log(f"MISSION session {session} end: steps={result['steps']} "
            f"moves={result['moves']} done={result['done']} "
            f"covered_urls={len(covered)}")
        if result["done"]:
            done = True
            break
        if result["steps"] <= 1 and result["moves"] == 0:
            log("MISSION stalled (empty session) — ending mission")
            break
    return {"steps": total_steps, "moves": total_moves, "done": done,
            "stopped": not done, "run_dir": last_run_dir,
            "sessions": session, "covered_urls": len(covered)}


def _replay(run_dir: str, step: int) -> int:
    """Offline replay: print a saved step's Jev answers without driving the browser."""
    import glob as _glob
    import json as _json

    from src.report import replay_payload

    if step <= 0:
        files = sorted(_glob.glob(os.path.join(run_dir, "step-*-answers.json")))
        log(f"REPLAY {run_dir}: {len(files)} saved steps")
        for f in files:
            log(f"  {os.path.basename(f)}")
        log("pass --replay-step N to inspect one")
        return 0 if files else 2
    data = replay_payload(run_dir, step)
    answers = data.get("answers", {})
    if not answers:
        log(f"REPLAY FAIL: no answers for step {step} in {run_dir}")
        return 2
    log(f"REPLAY step {step} answers:")
    log(_json.dumps(answers, ensure_ascii=False, indent=1)[:4000])
    return 0


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(
        prog="otc",
        description="open-typesafe-camoufox: plain-English task -> Jev-classified browser actions -> done.",
    )
    ap.add_argument("--url", required=True, help="Start URL to solve.")
    ap.add_argument("--task", default="", help='Instruction, e.g. "Sign in with {TWITTER_USERNAME} / {TWITTER_PASSWORD}". Placeholders resolve from .env.local at execution; values never log.')
    ap.add_argument("--steer", default="steer.txt", help="Steer file polled each step (stop | goto <url> | instruction).")
    ap.add_argument("--fps", type=float, default=3.0, help="Jev sample rate 2-5 (capture stays 60fps).")
    ap.add_argument("--confidence", type=float, default=0.7, help="Min Jev confidence to click.")
    ap.add_argument("--budget", type=float, default=120.0, help="Wall-clock budget seconds (0 = infinite).")
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--headless", action="store_true", help="No visible window (default is HEADED).")
    ap.add_argument("--preflight", action="store_true", help="Verify keys + headed browser launch, then exit.")
    ap.add_argument("--legacy-planner", action="store_true", help="Use the legacy GPT planner loop instead of the Jev-primary decide loop.")
    ap.add_argument("--min-confidence", type=float, default=0.4, help="Jev-primary: gate below this confidence (idle instead of acting).")
    ap.add_argument("--replay", default="", help="Run dir to replay offline, e.g. runs/20260917-092647.")
    ap.add_argument("--replay-step", type=int, default=0, help="Step number to replay (0 = list available steps).")
    ap.add_argument("--mission", action="store_true", help="Resume-until-done: stopped sessions relaunch with covered URLs seeded, sharing the step/wall-clock budget (max 10 sessions). For long collection missions that must not die on transient stalls.")
    ap.add_argument("--humanize", action="store_true", help="Opt into Camoufox browser-level mouse smoothing (default OFF: measured per-dispatch degradation wedging mouse ops within ~3 moves).")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    set_verbose(args.verbose)

    if args.replay:
        return _replay(args.replay, args.replay_step)

    if args.preflight:
        missing = _check_placeholders(args.task)
        if missing:
            log(f"PREFLIGHT FAIL: task references unset env vars: {missing}")
            return 4
        return asyncio.run(_preflight(args.url, HUMANIZE_LEVEL if args.humanize else False))

    if not os.environ.get("GROQ_API_KEY"):
        log("FATAL: GROQ_API_KEY missing. Refusing to start. Run --preflight to check the browser too.")
        return 4
    missing = _check_placeholders(args.task)
    if missing:
        log(f"FATAL: task references unset env vars: {missing}")
        log("Fill them in .env.local (never commit) or drop the placeholders from --task.")
        return 4

    if args.mission and args.legacy_planner:
        log("MISSION with --legacy-planner: running a single legacy session (flag applies to the decide loop).")
        args.mission = False
    result = asyncio.run(
        _mission(
            start_url=args.url,
            task=args.task,
            steer_file=args.steer,
            fps=args.fps,
            min_confidence=args.min_confidence,
            budget_s=args.budget,
            max_steps=args.max_steps,
            headless=args.headless,
            humanize=HUMANIZE_LEVEL if args.humanize else False,
        )
        if args.mission
        else (
            run_jev_session(
                start_url=args.url,
                task=args.task,
                steer_file=args.steer,
                fps=args.fps,
                confidence=args.confidence,
                budget_s=args.budget,
                max_steps=args.max_steps,
                headless=args.headless,
            )
            if args.legacy_planner
            else run_decide_session(
                start_url=args.url,
                task=args.task,
                steer_file=args.steer,
                fps=args.fps,
                min_confidence=args.min_confidence,
                budget_s=args.budget,
                max_steps=args.max_steps,
                headless=args.headless,
                humanize=HUMANIZE_LEVEL if args.humanize else False,
            )
        )
    )
    log(f"result steps={result['steps']} moves={result['moves']} done={result['done']} stopped={result['stopped']}")
    return 0 if result["done"] else 2


def cli() -> None:
    """Console-script entry (pyproject [project.scripts]: otc / open-typesafe-camoufox)."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())