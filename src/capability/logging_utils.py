"""Shared logging for the capability package. Mirrors run.py's logging_utils
but tags lines with [capability]. All lines print once to stdout AND append
to the session run.log via the shared sink (src/_log_sink.py)."""

from __future__ import annotations

import sys

from src import _log_sink


def log(msg: str, *, tag: str = "") -> None:
    """Flushed log line — stdout + session run.log.

    ``tag`` names the solver/method for auditability (e.g.
    ``[capability:captchakraken]`` instead of a blank ``[capability]``).
    Modules bind their tag once via ``functools.partial``; call sites
    stay ``log("...")``.
    """
    prefix = f"[capability:{tag}]" if tag else "[capability]"
    line = f"{prefix} {msg}"
    _log_sink.append(line)
    try:
        print(_log_sink.console_safe(line), flush=True)
    except UnicodeEncodeError:
        print(line.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8", "replace"), flush=True)


_verbose = False


def set_verbose(value: bool) -> None:
    """Toggle verbose logging. run.py's main() sets this on the capability
    package (this module) so --verbose covers both the CLI and the capability layer."""
    global _verbose
    _verbose = value


def log_verbose(msg: str) -> None:
    """Log to stdout + run.log so it reaches the terminal when stdout is piped."""
    if _verbose:
        line = f"[capability] {msg}"
        _log_sink.append(line)
        print(_log_sink.console_safe(line), flush=True)