"""browser package — the ONLY code that touches playwright/camoufox.

CamoufoxPlatform owns the async page handle, the asyncio lock, the nav
state machine, the cursor tracker lifecycle, idle motion, and the human
highlight ring. Everything above this package (perception, decide, writer,
actions, runner, report) is platform-blind: it calls CamoufoxPlatform
verbs and never imports playwright or camoufox directly.
"""

from __future__ import annotations

from .camoufox import CamoufoxPlatform

__all__ = ["CamoufoxPlatform"]
