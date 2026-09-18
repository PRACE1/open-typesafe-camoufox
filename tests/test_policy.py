"""Policy tests — challenge backend order is pure and total (offline).

No browser, no fakes: same observable state always yields the same
plan. Adding a backend (vision grid solver) means extending these
tables, not re-reading actions.py.
"""

from src.runner.policy import SHIELD_FAMILIES, plan_challenge


def test_open_followup_vision_first_then_audit():
    # Vision configured: solve attempt first, OCR capture as audit
    # fallback. Never a re-click (that closes the popup).
    assert plan_challenge(kind="checkbox", framed=True, followup_open=True,
                          family="recaptcha", paid_available=False,
                          vision_available=True) == ["captchakraken",
                                                     "ddddocr"]
    # No key: free OCR capture alone (unchanged behavior).
    assert plan_challenge(kind="checkbox", framed=True, followup_open=True,
                          family="recaptcha", paid_available=False,
                          vision_available=False) == ["ddddocr"]


def test_open_followup_routes_to_grid_alone():
    # Re-clicking would close the popup: grid capture is the WHOLE plan.
    assert plan_challenge(kind="checkbox", framed=True, followup_open=True,
                          family="recaptcha", paid_available=True) == ["ddddocr"]


def test_framed_family_runs_shield_then_toggle():
    assert plan_challenge(kind="checkbox", framed=True, followup_open=False,
                          family="cf_turnstile",
                          paid_available=False) == ["shield-bypass",
                                                    "toggle"]
    for family in SHIELD_FAMILIES:
        plan = plan_challenge(kind="checkbox", framed=True,
                              followup_open=False, family=family,
                              paid_available=False)
        assert plan[0] == "shield-bypass"


def test_unframed_or_unknown_family_skips_shield():
    assert plan_challenge(kind="checkbox", framed=False, followup_open=False,
                          family="recaptcha",
                          paid_available=False) == ["toggle"]
    assert plan_challenge(kind="checkbox", framed=True, followup_open=False,
                          family="hcaptcha",
                          paid_available=False) == ["toggle"]
    assert plan_challenge(kind="checkbox", framed=True, followup_open=False,
                          family=None,
                          paid_available=False) == ["toggle"]


def test_paid_is_last_and_only_when_configured():
    assert plan_challenge(kind="checkbox", framed=True, followup_open=False,
                          family="recaptcha",
                          paid_available=True) == ["shield-bypass", "toggle",
                                                   "2captcha-python"]
    plan = plan_challenge(kind="checkbox", framed=False, followup_open=False,
                          family=None, paid_available=False)
    assert "2captcha-python" not in plan


def test_non_checkbox_kinds_unchanged():
    assert plan_challenge(kind="slider", framed=False, followup_open=False,
                          family=None, paid_available=True) == ["drag"]
    assert plan_challenge(kind="captcha", framed=False, followup_open=False,
                          family=None, paid_available=False) == ["ocr"]
    assert plan_challenge(kind="none", framed=False, followup_open=False,
                          family=None, paid_available=False) == ["refuse"]


def test_ranking_reorders_by_ledger_evidence():
    from src.runner.policy import plan_challenge

    stats = {"capability:shield-bypass:checkbox": {"attempts": 4, "successes": 0,
                                   "rate": 0.0}}
    plan = plan_challenge(kind="checkbox", framed=True, followup_open=False,
                          family="recaptcha", paid_available=False,
                          stats=stats)
    assert plan == ["toggle", "shield-bypass"]  # proven failure sinks
    # No stats: default cheapest-first order preserved.
    assert plan_challenge(kind="checkbox", framed=True, followup_open=False,
                          family="recaptcha", paid_available=False,
                          stats={}) == ["shield-bypass", "toggle"]
    assert plan_challenge(kind="checkbox", framed=True, followup_open=False,
                          family="recaptcha", paid_available=False,
                          stats=None) == ["shield-bypass", "toggle"]


def test_next_action_stage_table():
    from src.runner.policy import MAX_AUTO_ATTEMPTS, next_action

    assert MAX_AUTO_ATTEMPTS == 3  # their ceiling, not a tunable
    assert next_action("checkbox")["action"] == "click_checkbox"
    assert next_action("image_grid")["action"] == "capture_tiles"
    assert next_action("slider")["action"] == "drag_slider"
    assert next_action("text")["action"] == "capture_text"
    # Second attempt: wait for the token instead of re-clicking.
    assert next_action("turnstile", 0)["action"] == "click_checkbox"
    assert next_action("turnstile", 1)["action"] == "wait_token"
    assert next_action("managed_challenge", 1)["action"] == "wait_clear"
    # Unknown stages inspect, never act.
    assert next_action("none")["action"] == "inspect"
    assert next_action("unknown")["action"] == "inspect"
    assert next_action("bogus")["action"] == "inspect"
