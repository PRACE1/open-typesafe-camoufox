"""
CamoufoxCapability: backward-compat alias for browser.CamoufoxPlatform.

New code imports CamoufoxPlatform from src.browser. This alias keeps
existing subclasses (JevCapability) and imports working unchanged.
"""

from __future__ import annotations

from ..browser.camoufox import CamoufoxPlatform


class CamoufoxCapability(CamoufoxPlatform):
    """Compat subclass — no extra behavior. Prefer CamoufoxPlatform."""


__all__ = ["CamoufoxCapability", "CamoufoxPlatform"]
