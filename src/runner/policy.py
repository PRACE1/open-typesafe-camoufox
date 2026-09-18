"""policy.py — challenge backend ORDER (pure; no browser, no dispatches).

The *mechanics* live in ``actions.challenge_control`` (probes, clicks,
polls, submits); this module owns the *order* they run in, as a pure
function of observable state. Same inputs → same plan, unit-testable
without fakes, and the next backend (vision grid solver) slots in as
one more plan step instead of another nested branch.

Step vocabulary — named after the actual library doing the work
(each stage implemented in actions.py):
  ddddocr         — open image follow-up goes to the ddddocr OCR
                    pipeline (never click)
  shield-bypass   — native family click + token poll (shield-bypass port)
  toggle          — verified/native checkbox dispatch (ours, no library)
  2captcha-python — paid submit + inject (key + proxy required)
  captchakraken   — hosted vision grid solve (Abyss experts)
  drag            — slider drag (unchanged path)
  ocr             — single-image OCR pipeline (unchanged path)
  refuse          — not a challenge control at all

Stage vocabulary mirrors ``capability.stage`` (ported from BetterWright's
STAGES): the classifier names what is on screen, this table names what to
do about it. ``plan_challenge`` stays the backend order for the checkbox
path; ``next_action`` is the per-stage strategy consulted with the attempt
index (their maxAutoStages ceiling: callers stop after 3 attempts).
"""

from __future__ import annotations

from ..capability import stage as _stage

SHIELD_FAMILIES = ("cf_turnstile", "recaptcha")

# Plan step → ledger solver name (trajectory/solver_ledger.py). Ranking
# reorders by past effectiveness; steps absent here keep plan position.
STEP_SOLVERS = {
    "shield-bypass": "capability:shield-bypass:checkbox",
    "toggle": "actions:toggle",
    "2captcha-python": "capability:2captcha-python:solve",
    "captchakraken": "capability:captchakraken:grid",
    "ddddocr": "capability:ddddocr:ocr",
}

# Automatic stages allowed in one solve: hand off after at most three
# distinct attempts (their maxAutoStages default AND ceiling).
MAX_AUTO_ATTEMPTS = 3

# Per-stage strategy table (their nextSolveAction): action name the worker
# executes + settle wait afterwards. Pure data — actions.py interprets it.
STAGE_ACTIONS: dict[str, dict[str, object]] = {
    _stage.CHECKBOX: {"action": "click_checkbox", "wait_ms": 2500,
                      "description": "Click the provider checkbox"},
    _stage.TURNSTILE: {"action": "click_checkbox", "wait_ms": 3000,
                       "description": "Click the Turnstile widget"},
    _stage.MANAGED: {"action": "click_verify", "wait_ms": 5000,
                     "description": "Click the managed-challenge verify "
                                    "control if present"},
    _stage.SLIDER: {"action": "drag_slider", "wait_ms": 2000,
                    "description": "Drag the slider/puzzle handle"},
    _stage.IMAGE_GRID: {"action": "capture_tiles", "wait_ms": 0,
                        "description": "Crop the puzzle and hand tiles "
                                       "to vision/OCR"},
    _stage.MOTION: {"action": "click_growing", "wait_ms": 1800,
                    "description": "Sample two frames, click the shape "
                                   "that grew, confirm Next"},
    _stage.TEXT: {"action": "capture_text", "wait_ms": 0,
                  "description": "Capture the text CAPTCHA for OCR"},
    _stage.INVISIBLE: {"action": "wait_token", "wait_ms": 4000,
                       "description": "Wait for the invisible widget "
                                      "to mint a token"},
}

# Second-attempt overrides: the first click didn't clear it, so wait for
# the token instead of clicking again (their attemptIndex branching).
STAGE_RETRY_ACTIONS: dict[str, dict[str, object]] = {
    _stage.TURNSTILE: {"action": "wait_token", "wait_ms": 5000,
                       "description": "Wait for Turnstile to issue "
                                      "a response token"},
    _stage.MANAGED: {"action": "wait_clear", "wait_ms": 5000,
                     "description": "Wait for the managed browser check "
                                    "to clear"},
}


def next_action(stage: str, attempt_index: int = 0) -> dict[str, object]:
    """Strategy for one classified stage + attempt number (pure).

    Unknown stages (none/unknown/anything else) return ``inspect``: look,
    don't touch. Never raises.
    """
    table = STAGE_RETRY_ACTIONS if attempt_index > 0 else {}
    step = table.get(stage, STAGE_ACTIONS.get(stage))
    if step is None:
        return {"action": "inspect", "wait_ms": 0,
                "description": "Inspect the challenge visually; "
                               "no automatic action available"}
    return dict(step)


def plan_challenge(*, kind: str, framed: bool, followup_open: bool,
                   family: str | None,
                   paid_available: bool,
                   vision_available: bool = False,
                   stats: dict | None = None) -> list[str]:
    """Ordered backend plan for one challenge_control call.

    Checkbox with an open image follow-up: hosted vision solve first
    (when a key is configured — it actually clears grids), then the
    free OCR capture for the audit trail. Re-clicking is never planned
    (it closes the popup — seen live). Otherwise cheapest-first:
    native shield solve (framed family only), plain toggle, paid solve
    last and only when configured.

    ``stats`` (from trajectory/solver_ledger.stats) reorders the steps
    by past effectiveness — repeated failures demote a solver instead
    of retrying it forever. Empty/None stats keep default order.
    """
    if kind == "slider":
        return ["drag"]
    if kind == "captcha":
        return ["ocr"]
    if kind != "checkbox":
        return ["refuse"]
    if followup_open:
        steps = (["captchakraken", "ddddocr"] if vision_available
                 else ["ddddocr"])
        return _ranked(steps, stats)
    steps = []
    if framed and (family or "none") in SHIELD_FAMILIES:
        steps.append("shield-bypass")
    steps.append("toggle")
    if paid_available:
        steps.append("2captcha-python")
    return _ranked(steps, stats)


def _ranked(steps: list[str], stats: dict | None) -> list[str]:
    """Stable reorder of plan steps by ledger effectiveness (desc).

    Unknown steps (drag/ocr/refuse plans never reach here, but be
    safe) keep position. No stats → identical order.
    """
    if not stats:
        return steps
    from ..trajectory.solver_ledger import rank_names

    ranked_solvers = rank_names(
        [STEP_SOLVERS[s] for s in steps if s in STEP_SOLVERS], stats)
    order = {name: i for i, name in enumerate(ranked_solvers)}
    return sorted(steps,
                  key=lambda s: order.get(STEP_SOLVERS.get(s, s),
                                          len(ranked_solvers)))
