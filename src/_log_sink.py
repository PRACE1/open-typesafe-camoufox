"""Single append sink for all solver logs: console + persistent run.log.

Both logging_utils modules (run + capability) funnel through here so one
session log file (runs/<ts>/run.log) captures the full decision +
diagnostic feed: planner feed, element maps, nav recovery, tracker health.
"""

from __future__ import annotations

import sys

_file = None


def set_file(path: str) -> None:
    global _file
    try:
        old = _file
        _file = open(path, "a", encoding="utf-8")
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
    except OSError:
        _file = None


def append(line: str) -> None:
    if _file is not None:
        try:
            _file.write(line + "\n")
            _file.flush()
        except OSError:
            pass


def console_safe(line: str) -> str:
    """Encode against the console's encoding, replacing what it can't show."""
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        line.encode(enc)
        return line
    except (UnicodeEncodeError, LookupError):
        return line.encode(enc, "replace").decode(enc, "replace")