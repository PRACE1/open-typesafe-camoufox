"""Runner-helper tests — settle, placeholders, loop-guard, phases (offline)."""

import pytest

from src.runner import (
    APPROVAL_FALLBACK, APPROVAL_MIN, BARE_CLICK_VETO_KINDS,
    CAPTCHA_MAX_ATTEMPTS, EFFECT_KINDS,
    OVERRIDABLE_KINDS, READING_COOLDOWN_STEPS, Phase, captcha_should_stop,
    credential_placeholder,
    fresh_tabs, loop_guard_trip, no_effect_trip, page_settled, phase_step,
    proposal_executable, reading_cooldown_active, resolve_proposal_action,
    should_override, should_submit_instead,
)
from src.runner import _apply_proposal as _apply_proposal_fn
from src.runner import _choice_is_vetoed as _vetoed_fn
from src.writer import ProposedAction


def test_page_settled_blank_and_loading():
    assert page_settled("") is False
    assert page_settled("   ") is False
    assert page_settled("Looking for results") is False
    assert page_settled("LOADING...") is False


def test_page_settled_real_text():
    assert page_settled("The Moon has water ice at the south pole.") is True


def test_page_settled_long_text_with_banner():
    body = "Results. " * 60 + "Looking for results in English? Change to English"
    assert len(body) >= 300
    assert page_settled(body) is True


def test_credential_placeholder_password():
    task = "Sign in with {TWITTER_USERNAME} / {TWITTER_PASSWORD}"
    assert credential_placeholder(task, want_password=True) == "{TWITTER_PASSWORD}"
    assert credential_placeholder(task, want_password=False) == "{TWITTER_USERNAME}"


def test_credential_placeholder_fallbacks():
    assert credential_placeholder("no placeholders here", want_password=True) is None
    assert credential_placeholder("{SOME_KEY}", want_password=True) == "{SOME_KEY}"
    assert credential_placeholder("{SOME_KEY}", want_password=False) == "{SOME_KEY}"


def test_loop_guard_trips_on_third_repeat():
    sig = ("click_item", 0, "https://a.example/")
    trip, run = loop_guard_trip(None, 0, sig)
    assert (trip, run) == (False, 1)
    trip, run = loop_guard_trip(sig, run, sig)
    assert (trip, run) == (False, 2)
    trip, run = loop_guard_trip(sig, run, sig)
    assert (trip, run) == (True, 3)


def test_loop_guard_resets_on_change():
    sig_a = ("click_item", 0, "https://a.example/")
    sig_b = ("click_item", 1, "https://a.example/")
    sig_nav = ("click_item", 0, "https://b.example/")
    _, run = loop_guard_trip(None, 0, sig_a)
    _, run = loop_guard_trip(sig_a, run, sig_a)
    trip, run = loop_guard_trip(sig_a, run, sig_b)
    assert (trip, run) == (False, 1)
    _, run = loop_guard_trip(sig_b, run, sig_a)
    trip, run = loop_guard_trip(sig_a, run, sig_nav)
    assert (trip, run) == (False, 1)


def test_phase_happy_path_walk():
    trail: list[str] = []
    for ph in [Phase.SEE, Phase.DECIDE, Phase.GATE, Phase.ACT,
               Phase.VERIFY, Phase.SEE, Phase.DECIDE, Phase.GATE,
               Phase.VERIFY, Phase.STOPPED]:
        phase_step(trail, ph)
    assert trail[-1] == "stopped" and trail.count("see") == 2


def test_phase_idle_path_skips_act():
    trail: list[str] = []
    for ph in [Phase.SEE, Phase.DECIDE, Phase.GATE, Phase.VERIFY,
               Phase.SEE, Phase.DECIDE, Phase.GATE, Phase.DONE]:
        phase_step(trail, ph)
    assert trail[-1] == "done"


def test_phase_illegal_moves_raise():
    with pytest.raises(ValueError):
        phase_step([], Phase.DECIDE)  # must start at SEE
    with pytest.raises(ValueError):
        phase_step(["see"], Phase.ACT)  # SEE -> ACT skips DECIDE+GATE
    with pytest.raises(ValueError):
        phase_step(["see", "decide", "gate", "done"], Phase.SEE)  # DONE terminal


def test_approval_threshold_and_overridable_kinds():
    assert APPROVAL_MIN == 0.5
    assert set(OVERRIDABLE_KINDS) == {"click_item", "type_at", "goto"}
    assert set(BARE_CLICK_VETO_KINDS) == {"input", "textarea", "select"}
    assert "type_at" not in EFFECT_KINDS  # typing never changes body text
    assert set(EFFECT_KINDS) == {"click_item", "press_enter", "press_escape", "refresh", "back", "challenge"}
    from src.runner import STOP_AFTER_NOOPS
    assert STOP_AFTER_NOOPS == 3


def test_proposal_executable():
    from src.deps import ElementRef
    els = [ElementRef(idx=0, kind="link"), ElementRef(idx=5, kind="textarea")]
    good_click = ProposedAction(question="q?", kind="click_item", item=0,
                                url=None, rationale="r")
    assert proposal_executable(good_click, els) is True
    bad_item = ProposedAction(question="q?", kind="click_item", item=9,
                              url=None, rationale="r")
    assert proposal_executable(bad_item, els) is False
    assert proposal_executable(None, els) is False
    wait_kind = ProposedAction(question="q?", kind="wait", item=None,
                               url=None, rationale="r")
    assert proposal_executable(wait_kind, els) is False
    goto = ProposedAction(question="q?", kind="goto", item=None,
                          url="https://a.example/", rationale="r")
    assert proposal_executable(goto, els) is True
    click_on_input = ProposedAction(question="q?", kind="click_item", item=5,
                                    url=None, rationale="r")
    assert proposal_executable(click_on_input, els) is False
    type_on_link = ProposedAction(question="q?", kind="type_at", item=0,
                                  url=None, rationale="r")
    assert proposal_executable(type_on_link, els) is False
    type_on_input = ProposedAction(question="q?", kind="type_at", item=5,
                                   url=None, rationale="r")
    assert proposal_executable(type_on_input, els) is True


def _prop(kind, item):
    return ProposedAction(question="q?", kind=kind, item=item,
                          url=None, rationale="r")


def test_should_override_disagreement_lean_yes():
    assert should_override(approval=0.61, proposed=_prop("click_item", 24),
                           choice_kind="click_item", choice_item=0,
                           executable=True) is True
    assert should_override(approval=0.49, proposed=_prop("click_item", 24),
                           choice_kind="click_item", choice_item=0,
                           executable=True) is False


def test_should_override_agreement_or_unexecutable():
    assert should_override(approval=0.95, proposed=_prop("click_item", 0),
                           choice_kind="click_item", choice_item=0,
                           executable=True) is False
    assert should_override(approval=0.95, proposed=_prop("click_item", 24),
                           choice_kind="click_item", choice_item=0,
                           executable=False) is False
    assert should_override(approval=0.95, proposed=None,
                           choice_kind="click_item", choice_item=0,
                           executable=True) is False


def test_fresh_tabs_reconciliation():
    assert fresh_tabs({1, 2}, {1, 2, 3}) == {3}
    assert fresh_tabs({1, 2}, {1, 2}) == set()
    assert fresh_tabs(set(), {1}) == {1}  # first sighting lists all


def test_no_effect_trip_strictly_consecutive():
    fp = ("https://a.example/", "abc123")
    other = ("https://a.example/", "def456")
    assert no_effect_trip(True, fp, fp, True, "click_item") is True
    assert no_effect_trip(False, fp, fp, True, "click_item") is False
    assert no_effect_trip(True, None, fp, True, "click_item") is False
    assert no_effect_trip(True, fp, other, True, "click_item") is False
    assert no_effect_trip(True, fp, fp, False, "click_item") is False
    assert no_effect_trip(True, fp, fp, True, "type_at") is False
    assert no_effect_trip(True, fp, fp, True, None) is False
    assert no_effect_trip(True, fp, fp, True, "heal_step5") is True
    assert no_effect_trip(True, fp, fp, True, "challenge") is True


def test_captcha_budget_try_then_honest_stop():
    assert CAPTCHA_MAX_ATTEMPTS == 8
    assert captcha_should_stop(0) is False
    assert captcha_should_stop(7) is False
    assert captcha_should_stop(8) is True
    assert captcha_should_stop(12) is True
    assert captcha_should_stop(8, cap=9) is False


def test_should_submit_instead():
    from src.deps import ElementRef
    els = [ElementRef(idx=5, kind="textarea", value_len=24),
           ElementRef(idx=6, kind="textarea", value_len=0)]
    url = "https://www.google.com/"
    assert should_submit_instead(5, els, (5, url), url) is True
    assert should_submit_instead(5, els, None, url) is False
    assert should_submit_instead(6, els, (6, url), url) is False
    assert should_submit_instead(5, els, (5, "https://other.example/"), url) is False
    assert should_submit_instead(9, els, (9, url), url) is False
    # Live DOM value covers snapshots that hide fill-state (Google).
    assert should_submit_instead(6, els, (6, url), url, "typed query") is True
    assert should_submit_instead(6, els, (6, url), url, "  ") is False


def test_resolve_proposal_action_routing():
    from src.runner import resolve_proposal_action
    from src.deps import ElementRef
    els = [ElementRef(idx=0, kind="textarea"), ElementRef(idx=7, kind="link")]
    good = ProposedAction(question="q?", kind="click_item", item=7,
                          url=None, rationale="r")
    # disagree + lean-yes -> override
    assert resolve_proposal_action(choice_kind="click_item", choice_item=0,
                                   elements=els, fits=0.9, proposed=good,
                                   executable=True, approval=0.6) == "override"
    # vetoed pick + viable proposal, weak approval -> fallback
    assert resolve_proposal_action(choice_kind="click_item", choice_item=0,
                                   elements=els, fits=0.9, proposed=good,
                                   executable=True, approval=0.45) == "fallback"
    # vetoed pick, no viable proposal -> neutral idle
    assert resolve_proposal_action(choice_kind="click_item", choice_item=0,
                                   elements=els, fits=0.9, proposed=None,
                                   executable=False, approval=0.1) == "mismatch-idle"
    # mismatched verb (low fits), no proposal -> neutral idle
    assert resolve_proposal_action(choice_kind="type_at", choice_item=0,
                                   elements=els, fits=0.2, proposed=None,
                                   executable=False, approval=0.0) == "mismatch-idle"
    # agreement, sound pick -> none
    assert resolve_proposal_action(choice_kind="click_item", choice_item=7,
                                   elements=els, fits=0.9, proposed=good,
                                   executable=True, approval=0.9) == "none"


def test_notes_url_count():
    from src.runner import notes_url_count
    notes = ["https://a.example/x :: excerpt one",
             "https://a.example/x :: excerpt two",
             "https://b.example/ :: other",
             "garbage without separator",
             ""]
    assert notes_url_count(notes) == 3
    assert notes_url_count([]) == 0


def test_choice_is_vetoed():
    from src.deps import ElementRef
    els = [ElementRef(idx=0, kind="textarea"), ElementRef(idx=1, kind="link")]
    assert _vetoed_fn(els, 0) is True
    assert _vetoed_fn(els, 1) is False
    assert _vetoed_fn(els, 9) is False
    assert _vetoed_fn(els, None) is False


def test_apply_proposal_rewrites_decision():
    from src.decide import JevDecision, Kind
    d = JevDecision(kind=Kind.WAIT, confidence=0.4, approval=0.62)
    p = ProposedAction(question="q?", kind="click_item", item=7,
                       url=None, rationale="r")
    out = _apply_proposal_fn(d, p)
    assert out.kind == Kind.CLICK_ITEM and out.element_idx == 7
    assert out.confidence == 0.62
    assert out.page_ready == d.page_ready  # Nouls preserved


def test_reading_cooldown_window():
    assert READING_COOLDOWN_STEPS == 4
    assert reading_cooldown_active(7, 10) is True
    assert reading_cooldown_active(10, 10) is True  # boundary inclusive
    assert reading_cooldown_active(11, 10) is False
    assert reading_cooldown_active(3, 0) is False  # never adopted


def test_escalation_target():
    from src.runner import escalation_target
    assert escalation_target(1, "https://a.example/x", "https://g.example/", set()) is None
    assert escalation_target(2, "https://a.example/x", "https://g.example/", set()) is None
    assert escalation_target(3, None, "https://g.example/", set()) is None
    assert escalation_target(3, "", "https://g.example/", set()) is None
    assert escalation_target(3, "https://a.example/x", "https://g.example/",
                             {"https://a.example/x"}) is None  # already tried
    assert escalation_target(3, "https://g.example/", "https://g.example/",
                             set()) is None  # self-target
    assert escalation_target(3, "https://a.example/x", "https://g.example/",
                             set()) == "https://a.example/x"
    assert escalation_target(5, "https://a.example/x", "https://g.example/",
                             set()) == "https://a.example/x"
