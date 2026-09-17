"""Runner-helper tests — settle, placeholders, loop-guard, phases (offline)."""

import pytest

from src.runner import (
    APPROVAL_MIN, BARE_CLICK_VETO_KINDS, EFFECT_KINDS, OVERRIDABLE_KINDS,
    READING_COOLDOWN_STEPS, Phase, credential_placeholder, fresh_tabs,
    loop_guard_trip, page_settled, phase_step, proposal_executable,
    reading_cooldown_active, should_override,
)
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
    assert set(EFFECT_KINDS) == {"click_item", "press_enter", "refresh"}


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


def test_reading_cooldown_window():
    assert READING_COOLDOWN_STEPS == 4
    assert reading_cooldown_active(7, 10) is True
    assert reading_cooldown_active(10, 10) is True  # boundary inclusive
    assert reading_cooldown_active(11, 10) is False
    assert reading_cooldown_active(3, 0) is False  # never adopted
