"""lessons.py — cross-run memory reads (moved out of report.py).

No writers here: the per-run notebook append (``append_memory``) stays
a report.py writer; this module owns the shared-lessons excerpt used
to brief each run's state packet. Pure move; behavior unchanged.
"""

from __future__ import annotations

import os

LESSONS_LIMIT = 1200  # chars of the shared lessons file injected per call


def load_lessons(root: str, limit: int = LESSONS_LIMIT) -> str:
    """Bounded excerpt of the shared cross-run lessons notebook.

    `.agent-memory/MEMORY.md` holds durable hard-won facts (this build's
    quirks, gate thresholds, loop shapes). Bounded like the doc's notebook
    model: latest snapshot per run, never dumped whole, so the packet can't
    bloat across 40-50 steps.
    """
    path = os.path.join(root, ".agent-memory", "MEMORY.md")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut if cut else text[:limit]
