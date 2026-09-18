"""Shared logging for open-typesafe-camoufox run package. Mirrors video-agent.

All lines print once to stdout AND append to the session run.log via the
shared sink (src/_log_sink.py) — the file keeps the exact unicode, the
console gets a best-effort rendering.
"""

from __future__ import annotations

import sys

from src import _log_sink


def log(msg: str, *, tag: str = "") -> None:
    """jev-solver feed line; ``tag`` appends hierarchy (``step:start``)."""
    prefix = f"[jev-solver:{tag}]" if tag else "[jev-solver]"
    line = f"{prefix} {msg}"
    _log_sink.append(line)
    try:
        print(_log_sink.console_safe(line), flush=True)
    except UnicodeEncodeError:
        print(line.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8", "replace"), flush=True)


_verbose = False


def set_verbose(value: bool) -> None:
    global _verbose
    _verbose = value


def log_verbose(msg: str) -> None:
    if _verbose:
        line = f"[jev-solver] {msg}"
        _log_sink.append(line)
        print(_log_sink.console_safe(line), flush=True)