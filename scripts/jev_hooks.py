"""Jev-backed git hooks: pre-commit classifies/maps files, pre-push ranks by issues.

Called from .git/hooks/* (installed via scripts/install-hooks.ps1 from
scripts/githooks/), or directly for testing:

  uv run python scripts/jev_hooks.py pre-commit [--strict] [--threshold 0.75]
  uv run python scripts/jev_hooks.py pre-push [--threshold 0.75]

Line detail: files scoring >= --line-floor get a per-window Noul scan, and
hot windows (>= --line-hot) get a kind Choice, so the report names exact
line ranges plus the inferred issue kind:

  0.84  src/capability/human_move.py  (177 lines)
        L102-152 (0.81): race-condition

Contract (never brick git):
- Missing toolchain/deps, missing TYPESAFE_API_KEY, or JEV_HOOKS_OFF=1
  -> heuristic fallback or silent pass, exit 0.
- pre-commit is advisory (exit 0) unless --strict.
- pre-push blocks (exit 1) only when the top FILE issue score >= threshold.
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
LINE_WINDOW = 50
LINE_FLOOR = 0.35
LINE_HOT = 0.6
MAX_WINDOWS = 20
MAX_KIND_CALLS = 3
MAX_LINE_SCAN_FILES = 10

ISSUE_NOUL = {
    "type": "noul",
    "instructions": "Does this Python source file likely contain a defect, bug, or risky pattern?",
    "criteria": {
        "true": "bug-prone constructs, unhandled errors, race conditions, secret leaks, or broken logic visible in the excerpt",
        "false": "clean, idiomatic code with no visible defects",
    },
}

ISSUE_KINDS = {
    "race-condition": "shared mutable state across tasks/threads without locking; check-then-act gaps",
    "unhandled-error": "exceptions that propagate uncaught, empty handlers hiding failures, IO without timeouts",
    "secret-exposure": "secrets, keys, or credentials in code, logs, or prompts",
    "logic-error": "wrong condition, off-by-one, inverted boolean, dead or unreachable path",
    "api-misuse": "wrong signature, ignored return contract, blocking call inside async code",
    "resource-leak": "unclosed handles, unbounded buffers, runaway retries",
    "input-validation": "untrusted input used unchecked (paths, URLs, shell, eval)",
    "none-of-these": "no defect visible; the range looks clean",
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


def _read_lines(path: str) -> list[str]:
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def _windows(lines: list[str], width: int = LINE_WINDOW) -> list[tuple[int, int, str]]:
    """Split into 1-indexed inclusive (start, end, text) ranges."""
    out = []
    for s in range(0, len(lines), width):
        chunk = lines[s:s + width]
        if chunk:
            out.append((s + 1, s + len(chunk), "\n".join(chunk)[:HEAD_CHARS]))
    return out


def _subsample(items: list, limit: int) -> list:
    """Evenly thin a list to at most `limit` entries, keeping order."""
    if len(items) <= limit or limit <= 0:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


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
    creds = _jev_client()
    if creds is None:
        return {}, False
    base, key, model = creds
    from src.decide import _post

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


def _jev_client() -> tuple[str, str, str] | None:
    """(base, key, model) or None when Jev is unreachable (offline-safe)."""
    from dotenv import load_dotenv

    load_dotenv(os.path.join(ROOT, ".env.local"))
    try:
        from src.decide import _env
    except Exception as exc:
        print(f"jev-hooks: cannot import Jev client ({exc}); heuristic fallback")
        return None
    base, key, model = _env()
    if not key:
        print("jev-hooks: TYPESAFE_API_KEY missing; heuristic fallback (non-blocking)")
        return None
    return base, key, model


async def _jev_window_scores(base: str, key: str, model: str, path: str,
                             windows: list[tuple[int, int, str]]) -> dict[tuple[int, int], float]:
    """One Noul per line range: does THIS range contain a defect?"""
    from src.decide import _post

    out: dict[tuple[int, int], float] = {}
    for start, end, text in windows:
        payload = {
            "model": model,
            "state": {"file": path, "lines": f"L{start}-L{end}", "code": text},
            "questions": {
                "range_has_issue": {
                    "type": "noul",
                    "instructions": f"Do lines L{start}-L{end} of this file contain a defect, bug, or risky pattern?",
                    "criteria": {
                        "true": "a concrete defect is visible in exactly these lines",
                        "false": "these lines look clean on their own",
                    },
                }
            },
        }
        try:
            data = await _post(payload, 30.0, base, key)
            raw = data.get("answers", {}).get("range_has_issue", {}).get("noul", 0.0)
            out[(start, end)] = min(max(float(raw or 0.0), 0.0), 1.0)
        except Exception as exc:
            print(f"jev-hooks: window call failed for {path} L{start}-L{end} ({exc})")
    return out


async def _jev_window_kinds(base: str, key: str, model: str, path: str,
                            hot: list[tuple[int, int, str, float]]) -> dict[tuple[int, int], str]:
    """Classify each hot range into one issue kind (Choice, with a none option)."""
    from src.decide import _post

    out: dict[tuple[int, int], str] = {}
    for start, end, text, _score in hot:
        payload = {
            "model": model,
            "state": {"file": path, "lines": f"L{start}-L{end}", "code": text},
            "questions": {
                "issue_kind": {
                    "type": "choice",
                    "instructions": f"What kind of issue, if any, is visible in lines L{start}-L{end}?",
                    "criteria": dict(ISSUE_KINDS),
                }
            },
        }
        try:
            data = await _post(payload, 30.0, base, key)
            kind = str(data.get("answers", {}).get("issue_kind", {}).get("choice", ""))
            out[(start, end)] = kind if kind in ISSUE_KINDS else "unclassified"
        except Exception as exc:
            print(f"jev-hooks: kind call failed for {path} L{start}-L{end} ({exc})")
            out[(start, end)] = "unclassified"
    return out


async def _jev_line_detail(base: str, key: str, model: str, path: str,
                           opts: dict) -> list[str]:
    """Exact-line outline for one file: hot ranges + inferred kinds."""
    lines = _read_lines(path)
    if not lines:
        return []
    windows = _subsample(_windows(lines, opts["window"]), MAX_WINDOWS)
    scored = await _jev_window_scores(base, key, model, path, windows)
    hot = [(s, e, t, scored[(s, e)])
           for (s, e, t) in windows
           if (s, e) in scored and scored[(s, e)] >= opts["line_hot"]]
    hot.sort(key=lambda r: r[3], reverse=True)
    hot = hot[:MAX_KIND_CALLS]
    kinds = await _jev_window_kinds(base, key, model, path, hot) if hot else {}
    return [f"L{s}-L{e} ({sc:.2f}): {kinds.get((s, e), 'unreviewed')}"
            for (s, e, _t, sc) in hot]


def _parse_opts(argv: list[str], threshold: float) -> dict:
    """Shared CLI flags for both commands."""
    opts: dict = {"threshold": threshold, "strict": "--strict" in argv,
                  "line_floor": LINE_FLOOR, "line_hot": LINE_HOT,
                  "window": LINE_WINDOW, "no_lines": "--no-lines" in argv,
                  "files": []}
    i = 0
    while i < len(argv):
        a = argv[i]
        try:
            if a == "--threshold" and i + 1 < len(argv):
                opts["threshold"] = float(argv[i + 1])
                i += 1
            elif a == "--line-floor" and i + 1 < len(argv):
                opts["line_floor"] = float(argv[i + 1])
                i += 1
            elif a == "--line-hot" and i + 1 < len(argv):
                opts["line_hot"] = float(argv[i + 1])
                i += 1
            elif a == "--window" and i + 1 < len(argv):
                opts["window"] = max(10, int(argv[i + 1]))
                i += 1
            elif a == "--files":
                while i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    i += 1
                    opts["files"].append(argv[i])
        except ValueError:
            pass
        i += 1
    return opts


def _report(files: list[str], scores: dict[str, float], live: bool,
            detail: dict[str, list[str]] | None = None) -> float:
    print(f"jev-hooks: {len(files)} file(s) checked (model={'jev' if live else 'heuristic'})")
    print(f"{'score':>6}  path")
    top = 0.0
    for path in files:
        head, nlines = _excerpt(path)
        score = scores.get(path, _heuristic_score(head) if not live else 0.0)
        top = max(top, score)
        print(f"{score:6.2f}  {path}  ({nlines} lines)")
        for line in (detail or {}).get(path, []):
            print(f"        {line}")
    return top


async def _scan_with_detail(files: list[str], opts: dict) -> tuple[float, bool]:
    """File scores plus exact-line detail for qualifying files.

    Returns (top_score, live). Line detail runs only for files at/above
    --line-floor (top MAX_LINE_SCAN_FILES by score) to bound Jev calls.
    """
    scores, live = await _jev_scores(files)
    detail: dict[str, list[str]] = {}
    if live and not opts["no_lines"]:
        ranked = sorted(files, key=lambda p: scores.get(p, 0.0), reverse=True)
        creds = _jev_client()
        scanned = 0
        for path in ranked:
            if scores.get(path, 0.0) < opts["line_floor"]:
                continue
            if scanned >= MAX_LINE_SCAN_FILES:
                break
            if creds is None:
                break
            detail[path] = await _jev_line_detail(*creds, path, opts)
            scanned += 1
    return _report(files, scores, live, detail), live


def cmd_precommit(argv: list[str]) -> int:
    opts = _parse_opts(argv, DEFAULT_THRESHOLD)
    files = opts["files"] or _staged_py()
    if not files:
        print("jev-hooks pre-commit: no staged Python files; nothing to map.")
        return 0
    top, _live = asyncio.run(_scan_with_detail(files, opts))
    print("jev-hooks pre-commit: map complete (advisory).")
    if opts["strict"] and top >= opts["threshold"]:
        print(f"jev-hooks pre-commit --strict: top score {top:.2f} >= {opts['threshold']}; blocking.")
        return 1
    return 0


def cmd_prepush(argv: list[str]) -> int:
    opts = _parse_opts(argv, DEFAULT_THRESHOLD)
    files = opts["files"] or _pushed_py()
    if not files:
        print("jev-hooks pre-push: no Python files in scope; nothing to rank.")
        return 0
    top, live = asyncio.run(_scan_with_detail(files, opts))
    if live and top >= opts["threshold"]:
        print(f"jev-hooks pre-push: BLOCKED — top issue score {top:.2f} >= {opts['threshold']}.")
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
