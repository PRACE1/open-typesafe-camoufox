"""Jev-backed git hooks: pre-commit classifies/maps files, pre-push ranks by issues.

Called from .git/hooks/* (installed via scripts/install-hooks.ps1 from
scripts/githooks/), or directly for testing:

  uv run python scripts/jev_hooks.py pre-commit [--strict] [--threshold 0.75]
  uv run python scripts/jev_hooks.py pre-push [--threshold 0.75]

Contract (never brick git):
- Missing toolchain/deps, missing TYPESAFE_API_KEY, or JEV_HOOKS_OFF=1
  -> heuristic fallback or silent pass, exit 0.
- pre-commit is advisory (exit 0) unless --strict.
- pre-push blocks (exit 1) only when the top issue score >= threshold.
  Bypass with `git push --no-verify`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(os.path.join(__file__, "..")))
sys.path.insert(0, ROOT)

DEFAULT_THRESHOLD = 0.75
MAX_FILES = 60
HEAD_LINES = 120
HEAD_CHARS = 4000

ISSUE_NOUL = {
    "type": "noul",
    "instructions": "Does this Python source file likely contain a defect, bug, or risky pattern?",
    "criteria": {
        "true": "bug-prone constructs, unhandled errors, race conditions, secret leaks, or broken logic visible in the excerpt",
        "false": "clean, idiomatic code with no visible defects",
    },
}


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _staged_py() -> list[str]:
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACM", "--", "*.py")
    return [p for p in out.splitlines() if p.strip()][:MAX_FILES]


def _pushed_py() -> list[str]:
    out = _git("log", "--name-only", "--pretty=format", "@{u}..HEAD", "--", "*.py")
    files = sorted({p for p in out.splitlines() if p.strip()})
    if not files:
        out = _git("diff", "--name-only", "HEAD~1", "HEAD", "--", "*.py")
        files = sorted({p for p in out.splitlines() if p.strip()})
    if not files:
        out = _git("ls-files", "*.py")
        files = sorted({p for p in out.splitlines() if p.strip()})
    return files[:MAX_FILES]


def _excerpt(path: str) -> tuple[str, int]:
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            lines = f.read().splitlines()
        head = "\n".join(lines[:HEAD_LINES])[:HEAD_CHARS]
        return head, len(lines)
    except OSError:
        return "", 0


def _heuristic_score(head: str) -> float:
    """Offline fallback when Jev is unreachable: keyword smell count."""
    flags = sum(
        head.count(k)
        for k in ("TODO", "FIXME", "XXX", "HACK", "except:", "except Exception",
                  "print(", "pragma: no cover", "nosec", "type: ignore")
    )
    return min(0.49, 0.10 + 0.05 * flags)


async def _jev_scores(files: list[str]) -> tuple[dict[str, float], bool]:
    """One Noul per file. Returns (scores, used_live_model)."""
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env.local"))
    try:
        from src.decide import _env, _post
    except Exception as exc:
        print(f"jev-hooks: cannot import Jev client ({exc}); heuristic fallback")
        return {}, False
    base, key, model = _env()
    if not key:
        print("jev-hooks: TYPESAFE_API_KEY missing; heuristic fallback (non-blocking)")
        return {}, False
    scores: dict[str, float] = {}
    for path in files:
        head, _nlines = _excerpt(path)
        if not head.strip():
            continue
        payload = {
            "model": model,
            "state": {"file": path, "excerpt": head},
            "questions": {"has_issue": ISSUE_NOUL},
        }
        try:
            data = await _post(payload, 30.0, base, key)
            raw = data.get("answers", {}).get("has_issue", {}).get("noul", 0.0)
            scores[path] = min(max(float(raw or 0.0), 0.0), 1.0)
        except Exception as exc:
            print(f"jev-hooks: Jev call failed for {path} ({exc}); heuristic fallback for it")
            scores[path] = _heuristic_score(head)
    return scores, True


def _report(files: list[str], scores: dict[str, float], live: bool) -> float:
    print(f"jev-hooks: {len(files)} file(s) checked (model={'jev' if live else 'heuristic'})")
    print(f"{'score':>6}  path")
    top = 0.0
    for path in files:
        head, nlines = _excerpt(path)
        score = scores.get(path, _heuristic_score(head) if not live else 0.0)
        top = max(top, score)
        print(f"{score:6.2f}  {path}  ({nlines} lines)")
    return top


def cmd_precommit(argv: list[str]) -> int:
    strict = "--strict" in argv
    threshold = DEFAULT_THRESHOLD
    for i, a in enumerate(argv):
        if a == "--threshold" and i + 1 < len(argv):
            try:
                threshold = float(argv[i + 1])
            except ValueError:
                pass
    files = _staged_py()
    if not files:
        print("jev-hooks pre-commit: no staged Python files; nothing to map.")
        return 0
    scores, live = asyncio.run(_jev_scores(files))
    top = _report(files, scores, live)
    print("jev-hooks pre-commit: map complete (advisory).")
    if strict and top >= threshold:
        print(f"jev-hooks pre-commit --strict: top score {top:.2f} >= {threshold}; blocking.")
        return 1
    return 0


def cmd_prepush(argv: list[str]) -> int:
    threshold = DEFAULT_THRESHOLD
    for i, a in enumerate(argv):
        if a == "--threshold" and i + 1 < len(argv):
            try:
                threshold = float(argv[i + 1])
            except ValueError:
                pass
    files = _pushed_py()
    if not files:
        print("jev-hooks pre-push: no Python files in scope; nothing to rank.")
        return 0
    scores, live = asyncio.run(_jev_scores(files))
    top = _report(files, scores, live)
    if live and top >= threshold:
        print(f"jev-hooks pre-push: BLOCKED — top issue score {top:.2f} >= {threshold}.")
        print("Fix the flagged file(s) or bypass with `git push --no-verify`.")
        return 1
    print("jev-hooks pre-push: clear.")
    return 0


def main(argv: list[str]) -> int:
    if os.environ.get("JEV_HOOKS_OFF") == "1":
        print("jev-hooks: JEV_HOOKS_OFF=1, skipping.")
        return 0
    if not argv or argv[0] not in ("pre-commit", "pre-push"):
        print(__doc__)
        return 2
    try:
        if argv[0] == "pre-commit":
            return cmd_precommit(argv[1:])
        return cmd_prepush(argv[1:])
    except Exception as exc:
        # Hooks must never brick git on unexpected errors.
        print(f"jev-hooks: unexpected error ({exc}); passing open.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
