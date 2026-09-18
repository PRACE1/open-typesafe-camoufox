"""runner package — the Jev-primary step loop, split by concern.

  constants.py  thresholds and vocabularies
  types.py      Phase enum + phase_step adapter
  helpers.py    pure helper functions
  gates.py      confidence gate, reading cooldown, recovery selection
  proposal.py   LLM proposal routing
  loop.py       run_decide_session and the step sub-functions

``run_decide_session`` and the pure helpers re-exported here keep
``from src.runner import ...`` working unchanged for callers.
"""

from .constants import (
    APPROVAL_FALLBACK,
    APPROVAL_MIN,
    BARE_CLICK_VETO_KINDS,
    CAPTCHA_MAX_ATTEMPTS,
    EFFECT_KINDS,
    GATE_EXEMPT_KINDS,
    OVERRIDABLE_KINDS,
    READING_COOLDOWN_STEPS,
    STOP_AFTER_NOOPS,
)
from .gates import confidence_gated, reading_cooldown_active, select_recovery
from .helpers import (
    captcha_should_stop,
    classify_noop,
    credential_placeholder,
    escalation_target,
    fresh_tabs,
    is_blank_page,
    loop_guard_trip,
    notes_url_count,
    no_effect_trip,
    page_settled,
    restart_candidates,
    screenshot_dead,
    screenshot_should_restart,
    should_submit_instead,
    step_verdict,
    stop_limits,
)
from .loop import run_decide_session
from .proposal import (
    _apply_proposal,
    _choice_is_vetoed,
    proposal_executable,
    resolve_proposal_action,
    should_override,
)
from .types import Phase, phase_step

__all__ = [
    "APPROVAL_FALLBACK",
    "APPROVAL_MIN",
    "BARE_CLICK_VETO_KINDS",
    "CAPTCHA_MAX_ATTEMPTS",
    "EFFECT_KINDS",
    "GATE_EXEMPT_KINDS",
    "OVERRIDABLE_KINDS",
    "READING_COOLDOWN_STEPS",
    "STOP_AFTER_NOOPS",
    "Phase",
    "phase_step",
    "captcha_should_stop",
    "classify_noop",
    "confidence_gated",
    "credential_placeholder",
    "escalation_target",
    "fresh_tabs",
    "is_blank_page",
    "loop_guard_trip",
    "notes_url_count",
    "no_effect_trip",
    "page_settled",
    "proposal_executable",
    "reading_cooldown_active",
    "restart_candidates",
    "resolve_proposal_action",
    "run_decide_session",
    "screenshot_dead",
    "screenshot_should_restart",
    "select_recovery",
    "should_override",
    "should_submit_instead",
    "stop_limits",
    "step_verdict",
    "_apply_proposal",
    "_choice_is_vetoed",
]
