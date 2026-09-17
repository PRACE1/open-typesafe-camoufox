"""
decide.py — Jev primary selector (typesafe's decide.py equivalent).

One TypeSafe request per step, three questions with mutually exclusive
options (overlapping options read as doubt and tank confidence):

  kind : Choice over the action verbs — wait | click_item | type_at |
         goto | done | none
  item : Choice over the element-map refs eN (used by click_item /
         type_at / challenge; decoded to the positional idx)
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
from .perception import build_state, host_of, norm_url


class Kind(str, Enum):
    WAIT = "wait"
    CLICK_ITEM = "click_item"
    TYPE_AT = "type_at"
    PRESS_ENTER = "press_enter"
    PRESS_ESCAPE = "press_escape"
    REFRESH = "refresh"
    BACK = "back"
    CLOSE_OTHERS = "close_others"
    GOTO = "goto"
    CHALLENGE = "challenge"
    DONE = "done"
    NONE = "none"


KIND_CRITERIA: dict[str, dict[str, str]] = {
    Kind.WAIT.value: {
        "what": "The page is loading, transitioning, or blank; deliberately do nothing and re-observe next step",
        "not_for": "Settled readable pages; any case another option describes",
    },
    Kind.CLICK_ITEM.value: {
        "what": "Click the chosen element (link/button) to navigate or trigger it; no text needed",
        "not_for": "Text inputs; submitting a typed query (press_enter); anything needing fresh text",
    },
    Kind.TYPE_AT.value: {
        "what": "Focus the chosen text input and type fresh text the writer composes; the field is cleared first",
        "not_for": "Buttons/links; credential fields without a task placeholder; retyping identical text into a filled field",
    },
    Kind.PRESS_ENTER.value: {
        "what": "Press Enter to submit the focused field, typically right after typing a query",
        "not_for": "Before any text was typed; dismissing dialogs",
    },
    Kind.PRESS_ESCAPE.value: {
        "what": "Press Escape once to dismiss an overlay, popup, or dialog covering the page",
        "not_for": "Submitting forms; any other key",
    },
    Kind.REFRESH.value: {
        "what": "Reload the current tab when its content failed to load or is visibly stale",
        "not_for": "Pages still loading (wait); navigating to a new URL",
    },
    Kind.BACK.value: {
        "what": "Browser-back to the previous page (e.g. article -> results) to continue hopping",
        "not_for": "First page of the run (empty history); reloading the same page",
    },
    Kind.CLOSE_OTHERS.value: {
        "what": "Close every tab except the current one and its opener; tab clutter blocks progress",
        "not_for": "Single-tab sessions; closing the tab being read",
    },
    Kind.GOTO.value: {
        "what": "Leave this page for a chosen catalog URL when the current page is finished",
        "not_for": "In-page actions; same-page retries",
    },
    Kind.CHALLENGE.value: {
        "what": "Work the chosen checkbox/slider challenge (consent, captcha-checkbox, slide-to-verify) blocking the task",
        "not_for": "Plain links/buttons/inputs (click_item/type_at); image puzzles — pick this anyway and the harness refuses those out loud",
    },
    Kind.DONE.value: {
        "what": "The TASK outcome is observably complete in PAGE TEXT; stop with the outcome",
        "not_for": "Loading or blank pages; partial progress without the concrete outcome",
        "note": "You do not write the outcome note — the harness assembles it from the pages you have read (your notes); your job is only to flag that you have read enough to name the outcome, even when the facts span several pages",
    },
    Kind.NONE.value: {
        "what": "No confident action exists; idle one step",
        "not_for": "Any case another option describes",
    },
}

KIND_INSTRUCTIONS = {
    "question": "Which single action advances the TASK?",
    "focus": "The options are mutually exclusive: pick exactly one. "
             "If the page is still loading, picking anything other than "
             "wait/none is wrong. Research tasks are completed in the main "
             "content region: header/nav chrome (search box, nav links) "
             "rarely advances the task once results are showing.",
}

MIN_CONFIDENCE = 0.4
MAX_ITEMS = 255  # Jev Choice cardinality cap


@dataclass
class JevDecision:
    kind: Kind
    element_idx: int | None = None
    target_url: str | None = None
    propose_url: bool = False
    confidence: float = 0.0
    # Noul flags (probability the answer is yes; no separate confidence —
    # near 1 is yes, near 0 is no, near 0.5 is uncertain).
    page_ready: float = 0.0
    needs_text: float = 0.0
    task_done: float = 0.0
    # Approval Noul on the LLM-posed question (0.0 when none was posed).
    approval: float = 0.0
    # Fits Noul on kind+item compatibility (defaults to trust).
    fits: float = 1.0
    # Score: position on the task-completion spectrum (0..1).
    progress: float = 0.0
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


def _item_option(e: ElementRef, visited: set[str]) -> dict[str, Any]:
    """Structured Choice option: the model sees what the element is, what it
    holds, where it points, and whether it was already visited — the full
    capability context for this ref, not just a label. Keyed by ephemeral
    ref (eN); the harness maps the chosen ref back to its idx."""
    label = e.label or e.placeholder or e.text or e.id or "?"
    state = "-"
    if e.kind in ("input", "textarea", "select") or e.type:
        state = f"filled({e.value_len}ch)" if e.value_len else "empty"
    box = ""
    if e.box is not None:
        box = ",".join(f"{v:.3f}" for v in e.box)
    return {
        "label": f"{e.ref}: {e.kind} \"{label}\"",
        "kind": e.kind,
        "type": e.type,
        "text": e.text,
        "href": e.href,
        "host": host_of(e.href),
        "region": e.region,
        "state": state,
        "at": f"{e.cx:.3f},{e.cy:.3f}",
        "box": box,
        "ref": e.ref,
        "visited": bool(e.href and norm_url(e.href) in visited),
    }


def build_questions(elements: list[ElementRef], sites: list[str],
                    visited: list[str] | None = None,
                    approval_question: str | dict | None = None) -> dict[str, Any]:
    """Three Choices plus three Noul flags plus one progress Score.

    Nouls flag situations needing a decision alongside the verb choice:
    page_ready (model-side settle check), needs_text (gate the writer),
    task_done (completion flag independent of kind=done). The Score places
    the run on the task-completion spectrum for long-horizon tracking.
    """
    vset = set(visited or [])
    item_criteria = {e.ref: _item_option(e, vset) for e in elements[:MAX_ITEMS]}
    if not item_criteria:
        item_criteria = {"-1": "No actionable elements on this page"}
    site_criteria: dict[str, Any] = {str(i): url[:160] for i, url in enumerate(sites)}
    site_criteria["other"] = {
        "what": "A different URL the writer proposes from the task",
        "not_for": "Any listed URL",
    }
    questions: dict[str, Any] = {
        "kind": {
            "type": "choice",
            "instructions": KIND_INSTRUCTIONS,
            "criteria": dict(KIND_CRITERIA),
        },
        "item": {
            "type": "choice",
            "instructions": {
                "question": "Which element should click_item/type_at/challenge act on?",
                "focus": "Used only for those kinds; still pick the best candidate. "
                         "Each option carries its ref, label, text, href, fill-state, "
                         "box, and visited mark.",
            },
            "criteria": item_criteria,
        },
        "site": {
            "type": "choice",
            "instructions": {
                "question": "Which URL should goto navigate to?",
                "focus": "Used only for goto; pick 'other' when the task needs a URL not listed here.",
            },
            "criteria": site_criteria,
        },
        "page_ready": {
            "type": "noul",
            "instructions": {
                "question": "Has the page finished loading?",
                "focus": "Blank pages, spinners, and 'looking for results' mean NO.",
            },
            "criteria": {
                "true": "Settled readable content is present",
                "false": "Blank, spinner, or results still loading",
            },
        },
        "needs_text": {
            "type": "noul",
            "instructions": {
                "question": "Does the next action need fresh free text typed?",
                "focus": "type_at almost always needs text; clicks, waits, and submits do not.",
            },
        },
        "task_done": {
            "type": "noul",
            "instructions": {
                "question": "Is the TASK observably complete in PAGE TEXT right now?",
                "focus": "Requires the concrete outcome (fact, confirmation) visible — not partial progress, not a loading page. Consider the NOTES from pages already read together with this page: if they collectively contain the outcome, answer YES. You do not compose any text; flagging is enough, the harness assembles the note from the pages you have read.",
            },
        },
        "fits": {
            "type": "noul",
            "instructions": {
                "question": "Does the chosen element suit the chosen action?",
                "focus": "click_item needs a link or button; type_at needs an empty or refillable text input. A text field about to be clicked, or a button about to be typed into, means NO.",
            },
            "criteria": {
                "true": "The element fits the verb",
                "false": "Wrong element kind for the verb",
            },
        },
        "progress": {
            "type": "score",
            "instructions": {
                "question": "How close is the TASK to complete?",
                "focus": "Judge observable page state against the task, not effort spent.",
            },
            "criteria": [
                "Nothing done yet",
                "Exploring / page loading",
                "Acting on the page",
                "Verifying the outcome",
                "Complete",
            ],
        },
    }
    if approval_question:
        questions["approval"] = {
            "type": "noul",
            "instructions": approval_question,
            "criteria": {
                "true": "Yes — take the proposed action now",
                "false": "No — the proposal is wrong or premature",
            },
        }
    return questions


class HealStrategy(str, Enum):
    """Recovery vocabulary for the heal state (decide_heal_action)."""

    REMAP_STALE = "remap_stale"
    DISMISS_COVER = "dismiss_cover"
    DRAG_SLIDER = "drag_slider"
    SOLVE_CHALLENGE = "solve_challenge"
    EXPAND_CAPABILITY = "expand_capability"
    ABORT = "abort"


HEAL_CRITERIA: dict[str, str] = {
    HealStrategy.REMAP_STALE.value: "Target moved or map re-rendered; re-probe and act on the fresh ref",
    HealStrategy.DISMISS_COVER.value: "Overlay, dropdown, or toast covers the target; dismiss it, then re-act",
    HealStrategy.DRAG_SLIDER.value: "Target is a drag handle or slide-to-verify control",
    HealStrategy.SOLVE_CHALLENGE.value: "Checkbox, Turnstile-style, or human-verification control blocks the task",
    HealStrategy.EXPAND_CAPABILITY.value: "A novel widget needs a synthesized capability (wallet popup, canvas, custom drag)",
    HealStrategy.ABORT.value: "Unrecoverable blocker; stop the run honestly",
}


async def decide_heal_action(*, error_msg: str, last_kind: str,
                             page_text: str,
                             timeout_s: float = 15.0) -> tuple[HealStrategy, float]:
    """Triage an execution failure into a recovery strategy + novelty score.

    One Jev request: a strategy Choice plus a need_new_cap Noul (probability
    the blocker needs a synthesized capability). No-key fallback returns
    (REMAP_STALE, 0.0) so the loop keeps its current remap behavior offline.
    """
    base, key, model = _env()
    if not key:
        return HealStrategy.REMAP_STALE, 0.0
    payload: dict[str, Any] = {
        "model": model,
        "state": {"error": (error_msg or "")[:300],
                  "last_action": last_kind,
                  "page_excerpt": (page_text or "")[:400]},
        "questions": {
            "strategy": {
                "type": "choice",
                "instructions": {
                    "question": "What recovery strategy resolves this execution failure?",
                    "focus": ("Pick expand_capability only when the page needs a widget "
                              "interaction no basic verb covers; prefer the concrete "
                              "remap/dismiss/drag/challenge options otherwise."),
                },
                "criteria": dict(HEAL_CRITERIA),
            },
            "need_new_cap": {
                "type": "noul",
                "instructions": {
                    "question": "Does this blocker require synthesizing a new browser capability?",
                    "focus": "YES only for novel widgets; NO for moved targets, overlays, sliders, and checkboxes.",
                },
            },
        },
    }
    try:
        data = await _post(payload, timeout_s, base, key)
    except Exception:
        return HealStrategy.REMAP_STALE, 0.0
    answers = data.get("answers", {}) if isinstance(data, dict) else {}
    strat_raw = str(answers.get("strategy", {}).get("choice", ""))
    try:
        strategy = HealStrategy(strat_raw)
    except ValueError:
        strategy = HealStrategy.REMAP_STALE
    try:
        need = float(answers.get("need_new_cap", {}).get("noul", 0.0) or 0.0)
    except (ValueError, TypeError):
        need = 0.0
    return strategy, min(max(need, 0.0), 1.0)


def ref_to_idx(choice: str, elements: list[ElementRef]) -> int:
    """Map a chosen item back to its positional idx.

    Accepts ephemeral refs (e4) and, for backward compatibility with cached
    prompts, bare ints (4). Returns -1 when unresolvable (sentinel → idle).
    """
    text = (choice or "").strip()
    if text.startswith("e"):
        text = text[1:]
    try:
        cand = int(text)
    except (ValueError, TypeError):
        return -1
    return cand if any(e.idx == cand for e in elements) else -1


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
    if kind in (Kind.CLICK_ITEM, Kind.TYPE_AT, Kind.CHALLENGE):
        item_raw = answers.get("item", {})
        cand = ref_to_idx(str(item_raw.get("choice", "-1")), elements)
        element_idx = cand if cand >= 0 else None

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

    def _noul(qid: str, default: float = 0.0) -> float:
        try:
            raw_v = answers.get(qid, {}).get("noul", default)
            return min(max(float(raw_v if raw_v is not None else default), 0.0), 1.0)
        except (ValueError, TypeError):
            return default

    try:
        progress = min(max(float(answers.get("progress", {}).get("score", 0.0) or 0.0), 0.0), 1.0)
    except (ValueError, TypeError):
        progress = 0.0

    return JevDecision(
        kind=kind, element_idx=element_idx, target_url=target_url,
        propose_url=propose_url,
        confidence=min(max(confidence, 0.0), 1.0),
        page_ready=_noul("page_ready"), needs_text=_noul("needs_text"),
        task_done=_noul("task_done"), approval=_noul("approval"),
        fits=_noul("fits", default=1.0),
        progress=progress, raw=decision_raw,
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
    lessons: str = "",
    blocked: str | None = None,
    approval_question: str | dict | None = None,
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
                        tabs=tabs, notes=notes, visited=visited, lessons=lessons,
                        blocked=blocked)
    payload: dict[str, Any] = {
        "model": model,
        "state": state,
        "questions": build_questions(elements, sites, visited, approval_question),
    }
    data = await _post(payload, timeout_s, base, key)
    return _decode(data, elements, sites)
