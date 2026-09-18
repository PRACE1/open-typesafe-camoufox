"""runner.constants — module-level thresholds and vocabularies for the step loop.

Tuning values live here so that helper functions and the main loop stay
declarative. All values are imported by name; see the inline comments for
the rationale behind each.
"""

from ..decide import Kind

# Approval thresholds ---------------------------------------------------
# Lower bar used ONLY when the Choice pick is vetoed (bare input click):
# a dead-certain dead click loses to an uncertain live proposal.
APPROVAL_MIN = 0.5
APPROVAL_FALLBACK = 0.4

# Overridable kinds ------------------------------------------------------
OVERRIDABLE_KINDS = ("click_item", "type_at", "goto")

# Bare-click veto --------------------------------------------------------
# Bare-clicking a text field never advances anything (no navigation, no
# state change — focusing happens inside type_at). Five straight runs
# fixated on the search box this way, so the runner vetoes it outright.
BARE_CLICK_VETO_KINDS = ("input", "textarea", "select")

# Effect kinds ------------------------------------------------------------
# Kinds whose lack of observable effect means failure. type_at is excluded
# on purpose: typing never changes body text, so every type would read as
# "no effect" (repeat-typing is the loop-guard's job, and clear-before-type
# keeps retypes idempotent).
EFFECT_KINDS = ("click_item", "press_enter", "press_escape", "refresh", "back", "challenge")

# Gate-exempt kinds -------------------------------------------------------
# Kinds the confidence gate never blocks: history reloads and dialog
# dismissal cannot type, post, pay, or navigate externally.
GATE_EXEMPT_KINDS = (Kind.DONE, Kind.BACK, Kind.REFRESH, Kind.PRESS_ESCAPE)

# Stop limits -------------------------------------------------------------
# Consecutive doubt no-ops before the run ends. High enough to survive a
# vetoed pick plus one gated wait; low enough that true fixation still
# ends the run instead of burning the whole budget.
STOP_AFTER_NOOPS = 3

# Reading cooldown ---------------------------------------------------------
# Steps a newly adopted result tab is protected from goto: the loop opened
# this page to READ it, so leaving immediately (the observed goto-back to
# Google) abandons the whole point. Rejections are neutral corrections,
# not no-ops — the other stop rails still terminate honestly.
READING_COOLDOWN_STEPS = 4

# CAPTCHA -----------------------------------------------------------------
# Consecutive image-challenge dead-ends before an honest stop. The runner
# must TRY (checkbox attempts, waits, re-observes, alternate routes),
# never stop on first contact; image grids are unsolvable without vision,
# so the budget bounds the attempts instead of hope.
CAPTCHA_MAX_ATTEMPTS = 8
