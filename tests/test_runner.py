"""Runner-helper tests — settle, placeholders, loop-guard, phases (offline)."""

import pytest

from src.runner import (
    APPROVAL_FALLBACK, APPROVAL_MIN, BARE_CLICK_VETO_KINDS,
    CAPTCHA_MAX_ATTEMPTS, EFFECT_KINDS, GATE_EXEMPT_KINDS,
    OVERRIDABLE_KINDS, READING_COOLDOWN_STEPS, Phase, captcha_should_stop,
    classify_noop, confidence_gated, credential_placeholder, fresh_tabs,
    is_blank_page, loop_guard_trip, no_effect_trip, page_settled,
    phase_step, proposal_executable, reading_cooldown_active,
    resolve_proposal_action, restart_candidates, screenshot_dead,
    screenshot_should_restart, select_recovery, should_override,
    should_submit_instead, step_verdict, stop_limits,
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


def test_confidence_gate_exempts_safe_navigation():
    from src.decide import Kind
    # Risky verbs idle below the floor ...
    assert confidence_gated(Kind.CLICK_ITEM, 0.2, 0.4) is True
    assert confidence_gated(Kind.GOTO, 0.2, 0.4) is True
    assert confidence_gated(Kind.PRESS_ENTER, 0.2, 0.4) is True
    assert confidence_gated(Kind.CHALLENGE, 0.2, 0.4) is True
    # ... but history reloads and dismissal can never exfiltrate, so the
    # gate lets them through (seen live: gated back idles killed a run).
    for kind in GATE_EXEMPT_KINDS:
        assert confidence_gated(kind, 0.0, 0.4) is False
    assert confidence_gated(Kind.CLICK_ITEM, 0.9, 0.4) is False


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


def test_is_blank_page_dead_load_only():
    from src.deps import ElementRef as _E
    assert is_blank_page([], "") is True
    assert is_blank_page([], "   ") is True
    assert is_blank_page([_E(idx=0, kind="link")], "") is False
    assert is_blank_page([], "x" * 50) is False
    assert is_blank_page([], "x" * 49) is True


def test_stop_limits_scale_with_budget():
    assert stop_limits(50) == (3, 2, 6)
    assert stop_limits(1) == (3, 2, 6)
    assert stop_limits(100) == (3, 2, 6)
    assert stop_limits(1000) == (20, 10, 20)
    assert stop_limits(500) == (10, 5, 10)


def test_restart_candidates_recent_first_no_current():
    urls = ["https://a.example/", "https://b.example/", "https://a.example/"]
    assert restart_candidates(urls, "https://b.example/") == ["https://a.example/"]
    assert restart_candidates(urls, "https://z.example/", limit=1) == ["https://a.example/"]
    assert restart_candidates([], "https://z.example/") == []


def test_screenshot_restart_schedule_and_dead_stop():
    assert screenshot_should_restart(0) is False
    assert screenshot_should_restart(2) is False
    assert screenshot_should_restart(3) is True
    assert screenshot_should_restart(4) is False
    assert screenshot_should_restart(6) is True
    assert screenshot_should_restart(9) is True
    assert screenshot_should_restart(12) is False  # dead, not restart
    assert screenshot_dead(11) is False
    assert screenshot_dead(12) is True


def test_classify_noop_maps_records_to_reasons():
    assert classify_noop("wait", None) == "idle"
    assert classify_noop("none", None) == "idle"
    assert classify_noop("type_at #5 (writer declined)", None) == "idle"
    assert classify_noop("challenge (no item)", None) == "idle"
    assert classify_noop("goto (no URL)", None) == "idle"
    assert classify_noop("anything", None, blank=True) == "blank"
    assert classify_noop("wait", None, blocked="sorry") == "blocked"
    assert classify_noop("loopguard wait (click_item #1)", None) == "fixation"
    assert classify_noop("mismatch wait (click_item #1)", None) == "mismatch"
    assert classify_noop("idle (low confidence)", None) == "lowconf"
    assert classify_noop("DONE rejected (empty page)", None) == "notready"
    assert classify_noop("goto rejected (reading)", None) == "cooldown"
    assert classify_noop("whatever", "error: element #1 target covered:div.x — skipping") == "covered"
    assert classify_noop("whatever", "error: nav failed: boom", same_fp=True) == "failed-same"
    assert classify_noop("whatever", "error: nav failed: boom", same_fp=False) == "failed"
    assert classify_noop("whatever", None) == "unknown"


def test_select_recovery_first_match():
    assert select_recovery("blank") == "refresh"
    assert select_recovery("failed-same") == "refresh"
    assert select_recovery("fixation") == "refresh"
    assert select_recovery("covered") == "escape"
    assert select_recovery("blocked") is None
    assert select_recovery("mismatch") is None
    assert select_recovery("lowconf") is None
    assert select_recovery("notready") is None
    assert select_recovery("cooldown") is None
    assert select_recovery("idle") is None
    assert select_recovery("failed") is None
    assert select_recovery("unknown") is None


def test_step_verdict_taxonomy():
    assert step_verdict(done=True, stopped=False, acted=True,
                        recovered=True, unresolved=False) == "complete"
    assert step_verdict(done=False, stopped=True, acted=True,
                        recovered=False, unresolved=False) == "terminal"
    assert step_verdict(done=False, stopped=False, acted=True,
                        recovered=False, unresolved=False) == "progress"
    assert step_verdict(done=False, stopped=False, acted=False,
                        recovered=True, unresolved=False) == "recoverable"
    assert step_verdict(done=False, stopped=False, acted=False,
                        recovered=False, unresolved=True) == "unresolved"
    assert step_verdict(done=False, stopped=False, acted=False,
                        recovered=False, unresolved=False) == "noop"


class _ShotFailPage:
    url = "https://a.example/"

    async def screenshot(self, type="png"):
        raise TimeoutError("taking page screenshot")


class _ShotFailPlatform:
    def __init__(self):
        self.page = _ShotFailPage()

    def tab_ids(self):
        return {1}

    async def tab_count(self):
        return 1

    async def go_back(self):
        return "went back to https://a.example/"


def _see_kwargs(tmp_path, **over):
    import src.report as _report

    kw = dict(platform=_ShotFailPlatform(), entry={"n": 1, "t": 1.0},
              steps=1, shot_streak=0, known_tab_ids={1},
              last_page_id=None, reading_until_step=0,
              visited=[], visited_set=set(), notes=[], history=[],
              task_text="t", run_dir=str(tmp_path), report_mod=_report,
              interval=0)
    kw.update(over)
    return kw


def test_step_see_screenshot_failure_skips_step(tmp_path):
    """Regression: plain screenshot failure (no restart/stop) must tell
    the caller to skip the step — the return carries no raw_png."""
    import asyncio as _asyncio

    from src.runner.loop import _step_see

    see = _asyncio.run(_step_see(**_see_kwargs(tmp_path)))
    assert see["skip_step"] is True
    assert see["restarted"] is False and see["stopped"] is False
    assert see["shot_streak"] == 1
    assert "raw_png" not in see  # caller must not touch observation keys


def test_step_see_restart_path_skips_step(tmp_path, monkeypatch):
    """Third consecutive failure triggers the Jev-scored restart branch
    (no-key fallback: history-back) and still skips the step."""
    import asyncio as _asyncio

    from src.runner.loop import _step_see

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    kw = _see_kwargs(tmp_path, shot_streak=2)
    see = _asyncio.run(_step_see(**kw))
    assert see["skip_step"] is True
    assert see["restarted"] is True and see["shot_streak"] == 3
    assert kw["entry"]["restart"]["action"] == "back"


class _VrPage:
    url = "https://a.example/"


def _vr_kwargs(tmp_path, **over):
    import src.report as _report

    kw = dict(machine=None, phase_trail=[], entry={"n": 1, "t": 1.0},
              steps=1, page=_VrPage(), page_text="Results. " * 60,
              elements=[], blocked=None, acted=False,
              done=False, stopped=False, stop_reason="",
              noops=0, noop_limit=3, dead_run=0, dead_limit=2,
              prev_fp=None, prev_acted=False, last_effect_kind=None,
              recoveries=[], recover_cap=3, same_fp=False,
              synth_attempted=True, captcha_streak=0, step_captcha=False,
              heal_edge=None, heal_abort=None, notes=[],
              effective_task="t", run_dir=str(tmp_path),
              report_mod=_report, history=[], platform=object())
    kw.update(over)
    return kw


def _drive(machine, trail, *events):
    from src.machine.run_engine import advance

    for ev in events:
        advance(machine, trail, ev)
    return trail


def test_propose_decide_returns_real_phase_timings(monkeypatch, tmp_path):
    """Regression: TIME decide=-3.1s. t_proposed/t_decided must be
    captured at phase boundaries (not once by the caller), so each
    phase duration is real and non-negative."""
    import asyncio as _asyncio
    import time as _time

    import src.runner.loop as _loop
    from src.decide import JevDecision, Kind
    from src.writer import ProposedAction

    async def _fake_propose(**kwargs):
        await _asyncio.sleep(0.02)
        return ProposedAction(question="Should the browser wait?",
                              kind="wait", item=None, url=None,
                              rationale="r")

    async def _fake_decide(**kwargs):
        await _asyncio.sleep(0.02)
        return JevDecision(kind=Kind.WAIT, confidence=0.9, raw={})

    async def _noop_advance(machine, trail, event):
        trail.append(event)
        return trail

    class _Page:
        url = "https://a.example/"

    class _Report:
        @staticmethod
        def write_answers_json(*a, **k):
            return "answers.json"

        @staticmethod
        def append_transcript(*a, **k):
            return None

    monkeypatch.setattr(_loop, "propose_action", _fake_propose)
    monkeypatch.setattr(_loop, "decide_action", _fake_decide)
    monkeypatch.setattr(_loop, "advance", _noop_advance)

    t0 = _time.time()
    pd = _asyncio.run(_loop._step_propose_decide(
        machine=object(), phase_trail=[], entry={"n": 1, "t": 1.0},
        steps=1, effective_task="t", start_url="https://a.example/",
        platform=object(), page=_Page(), elements=[], focused=None,
        page_text="settled body text here", history=[], n_tabs=1,
        notes=[], visited=[], lessons="", blocked=None,
        reading_until_step=0, mismatch_sig=None, mismatch_run=0,
        escalated_sigs=set(), escalated_urls=set(), last_typed=None,
        blank_streak=0, run_dir=str(tmp_path), report_mod=_Report(),
        frontier=None))
    t1 = _time.time()
    assert pd["idle"] is False and pd["decision"].kind == Kind.WAIT
    assert t0 <= pd["t_proposed"] <= pd["t_decided"] <= t1
    assert pd["t_decided"] - pd["t_proposed"] >= 0.015  # decide phase measured
    assert pd["t_proposed"] - t0 >= 0.015  # propose phase measured


def test_verify_recover_gate_stop_does_not_advance(tmp_path):
    """Regression: a GATE stop (loopguard fixation) leaves the machine
    in stopped, which has no outgoing edges — VERIFY must not advance
    (live crash: TransitionNotAllowed Can't Idle now when in Stopped)."""
    import asyncio as _asyncio

    from src.machine.run_engine import RunMachine
    from src.runner.loop import _step_verify_recover

    machine, trail = RunMachine(), []
    _drive(machine, trail, "perceived", "decided", "abort")
    assert trail[-1] == "stopped"
    vr = _asyncio.run(_step_verify_recover(**_vr_kwargs(
        tmp_path, machine=machine, phase_trail=trail, stopped=True,
        stop_reason="loopguard fixation on challenge #0 x6")))
    assert vr["stopped"] is True
    assert vr["stop_reason"] == "loopguard fixation on challenge #0 x6"
    assert len(trail) == 3  # no verify/recover transitions attempted


def test_verify_recover_done_skips_dead_rail_abort(tmp_path):
    """Regression: DONE accepted in GATE plus a tripped no-effect rail
    must not advance either (done has no outgoing edges)."""
    import asyncio as _asyncio

    from src import perception as _perception
    from src.machine.run_engine import RunMachine
    from src.runner.loop import _step_verify_recover

    machine, trail = RunMachine(), []
    _drive(machine, trail, "perceived", "decided", "finish")
    assert trail[-1] == "done"
    text = "Results. " * 60
    fp = _perception.page_fingerprint("https://a.example/", text)
    vr = _asyncio.run(_step_verify_recover(**_vr_kwargs(
        tmp_path, machine=machine, phase_trail=trail, done=True,
        page_text=text, dead_run=5, prev_fp=fp, prev_acted=True,
        last_effect_kind="click_item")))
    assert vr["done"] is True and vr["stopped"] is False
    assert len(trail) == 3  # no abort attempted from done


def test_heal_strategy_fits_challenge_only_controls():
    from src.runner.helpers import heal_strategy_fits

    # Checkbox/slider targets fit challenge strategies.
    assert heal_strategy_fits("solve_challenge", "checkbox", "robot") is True
    assert heal_strategy_fits("drag_slider", "slider", "") is True
    # Captcha-marked elements fit even with other kinds.
    assert heal_strategy_fits("solve_challenge", "image",
                              "select all images") is True
    assert heal_strategy_fits("solve_challenge", "button",
                              "verify you are human") is True
    # Plain elements and missing boxes never fit: the refusal in
    # challenge_control ("not a checkbox/slider") is guaranteed, so
    # dispatching is pure waste.
    assert heal_strategy_fits("solve_challenge", "link", "More") is False
    assert heal_strategy_fits("solve_challenge", "button", "Submit") is False
    assert heal_strategy_fits("solve_challenge", "input", "?") is False
    assert heal_strategy_fits("solve_challenge", "", "") is False
    assert heal_strategy_fits("drag_slider", "link", "x") is False
    # Non-challenge strategies fit everything.
    assert heal_strategy_fits("remap_stale", "link", "x") is True
    assert heal_strategy_fits("dismiss_cover", "", "") is True
    assert heal_strategy_fits("abort", "", "") is True


def test_short_result_keeps_tail_verdict():
    from src.runner.loop import _short_result

    assert _short_result(
        'element #5 ddddocr:{"a": 1} conf=0.00 '
        "awaiting JEV review") == \
        "element #5 conf=0.00 awaiting JEV review"
    assert _short_result("plain line") == "plain line"
    assert _short_result("") == "(empty)"
    assert len(_short_result("x" * 500)) <= 160


def test_jev_answer_summary_shows_every_question():
    from src.runner.loop import _jev_answer_summary

    raw = {"answers": {
        "kind": {"choice": "challenge", "confidence": 0.97},
        "item": {"choice": "f1e7", "confidence": 0.8},
        "site": {"choice": "other", "confidence": 0.5},
        "progress": {"score": 1.0},
        "broken": "not-a-dict",
        "vague": {"choice": "x"},
    }}
    assert _jev_answer_summary(raw) == {
        "kind": "challenge:0.97", "item": "f1e7:0.8",
        "site": "other:0.5", "vague": "x:0.0"}
    assert _jev_answer_summary(None) == {}
    assert _jev_answer_summary({}) == {}


def test_parse_rank_tail_extracts_order_and_rates():
    from src.runner.loop import _parse_rank_tail

    res = ('element #5 ok solver-rank:{"order": ["captchakraken", '
           '"ddddocr"], "rates": {"captchakraken": 0.8, "ddddocr": 0.0}}')
    rank = _parse_rank_tail(res)
    assert rank is not None
    assert rank["order"] == ["captchakraken", "ddddocr"]
    assert rank["rates"]["captchakraken"] == 0.8
    assert _parse_rank_tail("plain result line") is None
    assert _parse_rank_tail("solver-rank:not-json") is None
