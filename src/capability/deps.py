"""Dependencies injected into the pydantic-ai agent for the browser session."""

from __future__ import annotations

from dataclasses import dataclass, field

@dataclass
class CamoufoxDeps:
    """Dependencies injected into the agent for the browser session."""

    browser_urls: dict[str, str] = field(default_factory=dict)
    """Named URLs the agent may navigate to: prospect site, GBP, generated site."""

    log: list[str] = field(default_factory=list)
    """Ordered narration + action log; underpins the audio track."""

