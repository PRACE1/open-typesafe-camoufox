"""runner.proposal — LLM proposal routing (override / fallback / mismatch-idle / none).

The LLM writer proposes one candidate action and a yes/no approval
question; the Jev Choice vote classifies independently. This module
decides which of the two wins:
  * override — proposal disagrees with Choice + Noul leans yes → execute
  * fallback — Choice pick is vetoed/mismatched + viable proposal
  * mismatch-idle — vetoed/mismatched with no viable proposal → wait
  * none — agreement or no proposal; run the normal Choice path
"""

from __future__ import annotations

from dataclasses import replace

from ..decide import Kind
from ..writer import ProposedAction
from .constants import (
    APPROVAL_FALLBACK,
    APPROVAL_MIN,
    BARE_CLICK_VETO_KINDS,
    OVERRIDABLE_KINDS,
)


def proposal_executable(proposed: ProposedAction | None, elements: list,
                        frontier=None) -> bool:
    """True when an LLM-proposed candidate is safe to execute on approval.

    click_item needs a live, non-input idx (bare input clicks are vetoed
    downstream anyway — rejecting here keeps a misgrounded proposal from
    overriding a sound Choice); type_at needs an input-ish idx; goto needs
    nothing more (a missing URL falls through to the writer-proposal branch).
    All other kinds stay on the Choice path.

    ``frontier`` adds the revisit veto: a proposal aimed at an already-read
    target is never executable, so an approval-override can no longer
    re-click the same result every step (seen live: APHCI re-opened 4× via
    override). Rejected proposals fall back to the Choice vote or idle.
    """
    if proposed is None or proposed.kind not in OVERRIDABLE_KINDS:
        return False
    by_idx = {e.idx: e for e in elements}
    if proposed.kind == "click_item":
        if proposed.item is None or proposed.item not in by_idx:
            return False
        if by_idx[proposed.item].kind in BARE_CLICK_VETO_KINDS:
            return False
        if frontier is not None:
            try:
                if frontier.is_visited_element(by_idx[proposed.item]):
                    return False
            except Exception:
                pass
        return True
    if proposed.kind == "type_at":
        return (proposed.item is not None and proposed.item in by_idx
                and by_idx[proposed.item].kind in ("input", "textarea", "select"))
    if proposed.kind == "goto" and frontier is not None \
            and proposed.url:
        try:
            if frontier.is_visited_url(proposed.url):
                return False
        except Exception:
            pass
    return True  # goto


def should_override(*, approval: float, proposed: ProposedAction | None,
                    choice_kind: str, choice_item: int | None,
                    executable: bool) -> bool:
    """True when an LLM proposal should execute over a differing Choice vote.

    Agreement needs no override (the normal path runs). On disagreement a
    lean-yes (0.5) for the reasoning mind wins: it read every element, host,
    region, and note, while the classifier has a demonstrated fixation mode
    (five runs of search-box clicks at 0.7+). Guard, gate-on-agreement, and
    no-effect rails bound a wrong override to ~one wasted step.
    """
    if proposed is None or not executable:
        return False
    if (proposed.kind, proposed.item) == (choice_kind, choice_item):
        return False
    return approval >= APPROVAL_MIN


def resolve_proposal_action(*, choice_kind: str, choice_item: int | None,
                            elements: list, fits: float,
                            proposed: ProposedAction | None,
                            executable: bool, approval: float) -> str:
    """Route between the Choice vote and the LLM proposal.

    Returns 'override' (disagree + lean-yes: execute proposal),
    'fallback' (Choice pick vetoed/mismatched + viable proposal),
    'mismatch-idle' (vetoed/mismatched with no viable proposal: wait
    neutrally instead of executing known-dead), or 'none'.
    """
    if should_override(approval=approval, proposed=proposed,
                       choice_kind=choice_kind, choice_item=choice_item,
                       executable=executable):
        return "override"
    vetoed = (choice_kind == "click_item"
              and _choice_is_vetoed(elements, choice_item))
    mismatched = (choice_kind in ("click_item", "type_at")
                  and choice_item is not None and fits < 0.4)
    if (vetoed or mismatched) and proposed is not None and executable \
            and approval >= APPROVAL_FALLBACK:
        return "fallback"
    if vetoed or mismatched:
        return "mismatch-idle"
    return "none"


def _choice_is_vetoed(elements: list, idx: int | None) -> bool:
    """True when the Choice pick is a bare input click (would be vetoed)."""
    if idx is None:
        return False
    el = next((e for e in elements if e.idx == idx), None)
    return el is not None and el.kind in BARE_CLICK_VETO_KINDS


def _apply_proposal(decision, proposed: ProposedAction, elements=None):
    """Rewrite the decision to the approved/fallback proposal.

    Confidence is boosted past the gate; guard/verify rails still apply
    downstream, so a bad proposal costs ~one step, not the run.
    """
    ref = decision.item_ref
    if proposed.item is not None and elements is not None:
        hit = next((e for e in elements if e.idx == proposed.item), None)
        ref = hit.ref if hit is not None else None
    elif proposed.item is None:
        ref = None
    return replace(
        decision,
        kind=Kind(proposed.kind),
        element_idx=proposed.item
        if proposed.kind in ("click_item", "type_at", "challenge") else None,
        item_ref=ref,
        target_url=proposed.url if proposed.kind == "goto" else None,
        propose_url=proposed.kind == "goto" and proposed.url is None,
        confidence=max(decision.confidence, decision.approval),
    )
