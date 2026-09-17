"""
capability package — browser verbs, split by concern:

  logging_utils.py          log(), log_verbose(), set_verbose()
  deps.py                   CamoufoxDeps (compat; prefer src.deps RunState)
  scroll_math.py            pure scroll-trajectory math (ScrollStep, etc.)
  scroll_motion.py          async scroll execution against a real page
  cursor_tracker_script.py  the injected JS mouse tracker
  cursor_tracking.py        CursorTrackingMixin (tracker lifecycle, safe_goto)
  browser_actions.py        BrowserActionsMixin (highlight ring only; legacy
                            scroll/click/navigate/paginate verbs deleted)
  camoufox_capability.py    CamoufoxCapability (compat alias for
                            browser.CamoufoxPlatform, the single adapter)
  human_move.py             human trajectory primitives (HUMANIZE_LEVEL, ...)
  element_probe.py          ELEMENT_PROBE_JS (used by perception.find_elements)
  jev_actions.py            JevCapability (2-tool mixin; action bodies move to
                            actions.py, perception.py, writer.py over time)

Re-exports the symbols external code imports.
"""

from __future__ import annotations

from .camoufox_capability import CamoufoxCapability
from .deps import CamoufoxDeps
from ..browser.camoufox import CamoufoxPlatform
from ..deps import ElementRef, FocusedField, RunState

__all__ = ["CamoufoxCapability", "CamoufoxDeps", "CamoufoxPlatform", "ElementRef", "FocusedField", "RunState"]
