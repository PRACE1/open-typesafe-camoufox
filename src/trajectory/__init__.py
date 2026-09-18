"""trajectory package — offline trajectory reads, split out of report.py.

report.py owns byte-writers only. Everything that *reads* runs back
(replay, cross-run memory) lives here, so replay/verify tooling builds
on the trajectory package without importing writers.

Re-exported through src.report for backward compatibility.
"""

from __future__ import annotations

from .lessons import LESSONS_LIMIT, load_lessons
from .replay import replay_payload

__all__ = ["LESSONS_LIMIT", "load_lessons", "replay_payload"]
