"""
decide.py — Jev primary selector (typesafe's decide.py equivalent).

One TypeSafe request per step, three questions with mutually exclusive
options (overlapping options read as doubt and tank confidence):

  kind : Choice over the action verbs — wait | click_item | type_at |
         goto | done | none
  item : Choice over the element-map idx (used by click_item / type_at)
  site : Choice over the run's URL catalog + "other" (used by goto;
         "other" means the writer proposes a URL, code-revalidated)

Confidence comes from the kind choice. The runner gates on it
(min-confidence 0.4, two consecutive no-ops stop). Free text is NEVER
composed here — kind=type_at / site=other only routes to writer.py.

No-key fallback returns kind=none @ 0.0 so the loop stays testable offline.
Env is read at call time (load_env runs before the loop, after imports).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from .deps import ElementRef, FocusedField
from .perception import build_state, element_criteria


class Kind(str, Enum):
    WAIT = "wait"
    CLICK_ITEM = "click_item"
    TYPE_AT = "type_at"
    PRESS_ENTER = "press_enter"
    REFRESH = "refresh"
    CLOSE_OTHERS = "close_others"
    GOTO = "goto"
    DONE = "done"
    NONE = "none"


KIND_CRITERIA: dict[str, str] = {
    Kind.WAIT.value: "Page is loading or transitioning; do nothing this step and re-observe",
    Kind.CLICK_ITEM.value: "Click the chosen element (button/link); no text needed",
    Kind.TYPE_AT.value: "Focus the chosen input and type fresh text into it (writer composes it)",
    Kind.PRESS_ENTER.value: "Press Enter to submit the focused field (e.g. after typing)",
    Kind.REFRESH.value: "Reload the current tab; its content failed to load or is stale",
    Kind.CLOSE_OTHERS.value: "Close all tabs except the current one; too many tabs are open",
    Kind.GOTO.value: "This page is finished; navigate to the chosen site URL",
    Kind.DONE.value: "TASK is observably complete in PAGE TEXT; stop with the outcome",
    Kind.NONE.value: "No confident action; idle this step",
}

KIND_INSTRUCTIONS = (
    "Which single action advances the TASK? The options are mutually exclusive: "
    "pick exactly one. If the page is still loading, picking anything other "
    "than wait/none is wrong."
)

MIN_CONFIDENCE = 0.4
MAX_ITEMS = 255  # Jev Choice cardinality cap


@dataclass
class JevDecision:
    kind: Kind
    element_idx: int | None = None
    target_url: str | None = None
    propose_url: bool = False
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def need_text(self) -> bool:
        """Whether this decision routes to the writer for free text."""
        return self.kind == Kind.TYPE_AT or (self.kind == Kind.GOTO and self.propose_url)


def _env() -> tuple[str, str, str]:
    base = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai/v1")
    key = os.environ.get("TYPESAFE_API_KEY", "")
    model = os.environ.get("TYPESAFE_MODEL", "jev-latest")
    return base, key, model


async def _post(payload: dict, timeout_s: float, base: str, key: str) -> dict:
    headers = {"Authorization": f"Bearer {key}"}
    backoff = 1.0
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        for attempt in range(4):
            res = await client.post(
                f"{base.rstrip('/')}/systemone", json=payload, headers=headers
            )
            if res.status_code in (429, 529) and attempt < 3:
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            res.raise_for_status()
            return res.json()
    raise RuntimeError("unreachable")


def build_questions(elements: list[ElementRef], sites: list[str],
                    visited: list[str] | None = None) -> dict[str, Any]:
    """The three Choice questions for one Jev request."""
    vset = set(visited or [])
    item_criteria = {str(e.idx): element_criteria(e, vset) for e in elements[:MAX_ITEMS]}
    if not item_criteria:
        item_criteria = {"-1": "No actionable elements on this page"}
    site_criteria = {str(i): url[:160] for i, url in enumerate(sites)}
    site_criteria["other"] = "A different URL the writer will propose from the task"
    return {
        "kind": {
            "type": "choice",
            "instructions": KIND_INSTRUCTIONS,
            "criteria": dict(KIND_CRITERIA),
        },
        "item": {
            "type": "choice",
            "instructions": (
                "Which element should click_item/type_at act on? "
                "Used only for those kinds; pick the best candidate anyway."
            ),
            "criteria": item_criteria,
        },
        "site": {
            "type": "choice",
            "instructions": (
                "Which URL should goto navigate to? Used only for goto; "
                "pick 'other' when the task needs a URL not listed here."
            ),
            "criteria": site_criteria,
        },
    }


def _decode(decision_raw: dict, elements: list[ElementRef], sites: list[str]) -> JevDecision:
    answers = decision_raw.get("answers", {})
    kind_raw = answers.get("kind", {})
    kind_val = str(kind_raw.get("choice", Kind.NONE.value))
    try:
        kind = Kind(kind_val)
    except ValueError:
        kind = Kind.NONE
    try:
        confidence = float(kind_raw.get("confidence", 0.0) or 0.0)
    except (ValueError, TypeError):
        confidence = 0.0

    element_idx: int | None = None
    if kind in (Kind.CLICK_ITEM, Kind.TYPE_AT):
        item_raw = answers.get("item", {})
        try:
            cand = int(str(item_raw.get("choice", "-1")))
        except (ValueError, TypeError):
            cand = -1
        valid = {e.idx for e in elements}
        element_idx = cand if cand in valid else None

    target_url: str | None = None
    propose_url = False
    if kind == Kind.GOTO:
        site_raw = answers.get("site", {})
        site_val = str(site_raw.get("choice", "other"))
        if site_val == "other":
            propose_url = True
        else:
            try:
                target_url = sites[int(site_val)]
            except (ValueError, IndexError):
                propose_url = True

    return JevDecision(
        kind=kind, element_idx=element_idx, target_url=target_url,
        propose_url=propose_url,
        confidence=min(max(confidence, 0.0), 1.0), raw=decision_raw,
    )


async def decide_action(
    *,
    task: str,
    url: str,
    start_url: str,
    elements: list[ElementRef],
    focused: FocusedField,
    page_text: str,
    history: list[str],
    frame: str = "",
    grid: str = "",
    tabs: int = 1,
    notes: list[str] | None = None,
    visited: list[str] | None = None,
    timeout_s: float = 30.0,
) -> JevDecision:
    """One Jev request -> the single next action (+ confidence)."""
    base, key, model = _env()
    if not key:
        return JevDecision(kind=Kind.NONE, confidence=0.0, raw={"fallback": "no-key"})

    sites: list[str] = []
    for cand in (start_url, url):
        if cand and cand not in sites:
            sites.append(cand)

    state = build_state(task=task, url=url, elements=elements, focused=focused,
                        page_text=page_text, history=history, frame=frame, grid=grid,
                        tabs=tabs, notes=notes, visited=visited)
    payload: dict[str, Any] = {
        "model": model,
        "state": state,
        "questions": build_questions(elements, sites, visited),
    }
    data = await _post(payload, timeout_s, base, key)
    return _decode(data, elements, sites)
