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
import os
import re
import sys

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


async def _preflight(url: str) -> int:
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
            async with AsyncCamoufox(headless=False, humanize=HUMANIZE_LEVEL) as browser:
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
    ap.add_argument("--budget", type=float, default=120.0, help="Wall-clock budget seconds.")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--headless", action="store_true", help="No visible window (default is HEADED).")
    ap.add_argument("--preflight", action="store_true", help="Verify keys + headed browser launch, then exit.")
    ap.add_argument("--legacy-planner", action="store_true", help="Use the legacy GPT planner loop instead of the Jev-primary decide loop.")
    ap.add_argument("--min-confidence", type=float, default=0.4, help="Jev-primary: gate below this confidence (idle instead of acting).")
    ap.add_argument("--replay", default="", help="Run dir to replay offline, e.g. runs/20260917-092647.")
    ap.add_argument("--replay-step", type=int, default=0, help="Step number to replay (0 = list available steps).")
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
        return asyncio.run(_preflight(args.url))

    if not os.environ.get("GROQ_API_KEY"):
        log("FATAL: GROQ_API_KEY missing. Refusing to start. Run --preflight to check the browser too.")
        return 4
    missing = _check_placeholders(args.task)
    if missing:
        log(f"FATAL: task references unset env vars: {missing}")
        log("Fill them in .env.local (never commit) or drop the placeholders from --task.")
        return 4

    result = asyncio.run(
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
        )
    )
    log(f"result steps={result['steps']} moves={result['moves']} done={result['done']} stopped={result['stopped']}")
    return 0 if result["done"] else 2


def cli() -> None:
    """Console-script entry (pyproject [project.scripts]: otc / open-typesafe-camoufox)."""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())