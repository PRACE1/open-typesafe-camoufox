"""Typed run state — replaces CamoufoxDeps + the __jev_* string tuples.

browser_urls["__jev_last__"] ("x,y,conf"), ["__jev_frame__"] (braille text)
and ["__jev_grid__"] ("80x20") were comma-joined strings smuggled through a
URL dict. RunState carries the same facts as typed fields; `browser_urls`
is kept as a plain dict for URL storage only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CamoufoxDeps:
    """Dependencies injected into the agent for the browser session."""

    browser_urls: dict[str, str] = field(default_factory=dict)
    """Named URLs the agent may navigate to."""

    log: list[str] = field(default_factory=list)
    """Ordered narration + action log."""


@dataclass
class ElementRef:
    """One entry of the element map (perception.find_elements).

    ref is the model-facing ephemeral handle (e0, e1, ...), valid for one
    probe only — the playwright-cli contract. idx is the positional index
    (int(ref[1:])); internal code keys on idx, models choose refs. box is
    the viewport-normalized rect (x, y, w, h in 0..1 units) at probe time;
    None when unknown. Callers scale by the live viewport for pixels. sel
    is the read-only durable selector generated at probe time (id / name /
    aria-label / structural path) for order-independent resolution; it is
    internal only, never shown to models and never written to the DOM.
    """

    idx: int
    kind: str
    ref: str = ""
    box: tuple[float, float, float, float] | None = None
    sel: str = ""
    type: str = ""
    id: str = ""
    label: str = ""
    placeholder: str = ""
    text: str = ""
    value: str = ""
    value_len: int = 0
    href: str = ""
    region: str = ""
    cx: float = 0.5
    cy: float = 0.5


@dataclass
class FocusedField:
    """The currently focused field (document.activeElement)."""

    role: str = ""
    label: str = ""
    placeholder: str = ""
    value: str = ""
    input_type: str = ""
    is_credential: bool = False
    frame: dict[str, int] = field(default_factory=dict)


@dataclass
class RunState:
    """Everything the decider, writer, and runner need for one step."""

    task: str = ""
    url: str = ""
    elements: list[ElementRef] = field(default_factory=list)
    focused: FocusedField = field(default_factory=FocusedField)
    page_text: str = ""
    frame: str = ""  # braille texture (optional secondary state)
    grid: str = ""
    hint_x: float = 0.5
    hint_y: float = 0.45
    hint_confidence: float = 0.0
    history: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    browser_urls: dict[str, str] = field(default_factory=dict)
