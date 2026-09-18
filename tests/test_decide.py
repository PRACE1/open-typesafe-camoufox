"""Decide tests — mutual exclusion, caps, decode, no-key fallback (all offline)."""

import asyncio

from src.decide import (
    HEAL_CRITERIA, HealStrategy, Kind, KIND_CRITERIA, MAX_ITEMS, JevDecision,
    build_questions, decide_action, decide_heal_action,
    decide_recovery_action, decide_restart_action, ref_to_idx,
)
from src.deps import ElementRef, FocusedField


def _els(n):
    return [ElementRef(idx=i, kind="link", label=f"l{i}", ref=f"e{i}",
                       box=(0.01, 0.05, 0.1, 0.05), cx=0.1, cy=0.1)
            for i in range(n)]


def test_kinds_mutually_exclusive():
    assert len(KIND_CRITERIA) == len({k.value for k in Kind}) == 12
    assert set(KIND_CRITERIA) == {k.value for k in Kind}


def test_kind_criteria_have_boundaries():
    for kind in Kind:
        crit = KIND_CRITERIA[kind.value]
        assert set(crit) >= {"what", "not_for"}, kind
        assert crit["what"] and crit["not_for"]
    assert "harness assembles" in KIND_CRITERIA[Kind.DONE.value]["note"]


def test_questions_cover_choices_nouls_score():
    q = build_questions(_els(2), ["https://a.example"])
    assert set(q) == {"kind", "item", "site", "page_ready", "needs_text",
                      "task_done", "fits", "progress"}
    assert q["page_ready"]["type"] == "noul"
    assert q["needs_text"]["type"] == "noul"
    assert q["task_done"]["type"] == "noul"
    assert q["progress"]["type"] == "score"
    assert isinstance(q["progress"]["criteria"], list)
    assert "approval" not in q


def test_approval_noul_present_only_when_posed():
    q = build_questions(_els(1), ["https://a.example"],
                        approval_question="Should the browser click #0?")
    assert q["approval"]["type"] == "noul"
    assert q["approval"]["instructions"] == "Should the browser click #0?"


def test_approval_structured_instructions():
    struct = {"question": "Should the browser click #0?",
              "candidate": {"kind": "click_item", "item": 0, "url": None},
              "rationale": "Top result.",
              "focus": "YES only if best."}
    q = build_questions(_els(1), ["https://a.example"], approval_question=struct)
    assert q["approval"]["instructions"] == struct


def test_approval_decode_and_default():
    from src.decide import _decode
    raw = _raw("click_item", item="1")
    raw["answers"]["approval"] = {"noul": 0.85}
    assert _decode(raw, _els(3), ["https://a.example"]).approval == 0.85
    assert _decode(_raw("wait"), _els(1), ["https://a.example"]).approval == 0.0


def test_item_options_are_structured():
    from src.deps import ElementRef as _E
    els = [_E(idx=0, kind="input", type="text", label="Search", ref="e0",
              box=(0.01, 0.05, 0.1, 0.05),
              text="", href="", cx=0.5, cy=0.4, value="q", value_len=1)]
    q = build_questions(els, ["https://a.example"],
                        visited=["https://other.example/"])
    assert set(q["item"]["criteria"]) == {"e0"}
    opt = q["item"]["criteria"]["e0"]
    assert opt["label"] == 'e0: input "Search"' and opt["state"] == "filled(1ch)"
    assert opt["ref"] == "e0" and opt["visited"] is False
    # No coordinates in options (aria identity; boxes resolve lazily at ACT).
    assert "box" not in opt and "at" not in opt and "sel" not in opt


def test_item_choice_caps_at_255():
    q = build_questions(_els(300), ["https://a.example"])
    assert len(q["item"]["criteria"]) == MAX_ITEMS == 255


def test_item_choice_empty_page_has_sentinel():
    q = build_questions([], ["https://a.example"])
    assert q["item"]["criteria"] == {"-1": "No actionable elements on this page"}


def test_site_choice_has_other():
    q = build_questions(_els(2), ["https://a.example", "https://b.example"])
    other = q["site"]["criteria"]["other"]
    assert other["what"].startswith("A different URL")


def _raw(kind, item="1", site="0", conf=0.9):
    return {"answers": {
        "kind": {"choice": kind, "confidence": conf},
        "item": {"choice": item},
        "site": {"choice": site},
    }}


def _raw_full(kind, ready=0.9, text=0.1, done=0.0, prog=0.4, **kw):
    raw = _raw(kind, **kw)
    raw["answers"]["page_ready"] = {"noul": ready}
    raw["answers"]["needs_text"] = {"noul": text}
    raw["answers"]["task_done"] = {"noul": done}
    raw["answers"]["progress"] = {"score": prog}
    return raw


def test_noul_and_score_decode():
    d = _decode(_raw_full("type_at", ready=0.93, text=0.88, done=0.12, prog=0.6), n=3)
    assert d.page_ready == 0.93 and d.needs_text == 0.88
    assert d.task_done == 0.12 and d.progress == 0.6
    assert d.fits == 1.0  # absent Noul defaults to trust
    raw = _raw_full("click_item")
    raw["answers"]["fits"] = {"noul": 0.2}
    assert _decode(raw, n=3).fits == 0.2


def test_noul_score_defaults_when_missing():
    d = _decode(_raw("wait"), n=1)
    assert d.page_ready == 0.0 and d.needs_text == 0.0
    assert d.task_done == 0.0 and d.progress == 0.0


def _decode(raw, n=3):
    from src.decide import _decode
    sites = ["https://a.example", "https://b.example"]
    return _decode(raw, _els(n), sites)


def test_decode_click_item():
    d = _decode(_raw("click_item", item="e2"))
    assert d.kind == Kind.CLICK_ITEM and d.element_idx == 2 and d.confidence == 0.9
    assert d.need_text is False


def test_decode_bare_int_item_compat():
    d = _decode(_raw("click_item", item="2"))
    assert d.element_idx == 2


def test_decode_challenge_item():
    d = _decode(_raw("challenge", item="e1"))
    assert d.kind == Kind.CHALLENGE and d.element_idx == 1


def test_decode_bad_ref_is_none():
    assert _decode(_raw("click_item", item="e9")).element_idx is None
    assert _decode(_raw("click_item", item="bogus")).element_idx is None


def test_decode_matches_native_aria_ref():
    from src.deps import ElementRef as _E
    els = [_E(idx=0, kind="checkbox", label="Bot", ref="f3e7", aria="f3e7"),
           _E(idx=1, kind="link", label="Why", ref="f2e11", aria="f2e11")]
    raw = _raw("challenge", item="f3e7")
    from src.decide import _decode as _dec
    d = _dec(raw, els, ["https://a.example"])
    assert d.kind == Kind.CHALLENGE and d.element_idx == 0


def test_decode_type_at_needs_text():
    d = _decode(_raw("type_at", item="0"))
    assert d.kind == Kind.TYPE_AT and d.element_idx == 0 and d.need_text is True


def test_decode_bad_item_idx_is_none():
    d = _decode(_raw("click_item", item="99"))
    assert d.element_idx is None


def test_decode_unknown_kind_is_none():
    d = _decode(_raw("teleport"))
    assert d.kind == Kind.NONE


def test_decode_goto_site():
    d = _decode(_raw("goto", site="1"))
    assert d.kind == Kind.GOTO and d.target_url == "https://b.example"
    assert d.need_text is False


def test_decode_goto_other_proposes():
    d = _decode(_raw("goto", site="other"))
    assert d.kind == Kind.GOTO and d.propose_url is True and d.need_text is True


def test_confidence_clamped():
    d = _decode(_raw("wait", conf=9.9))
    assert d.confidence == 1.0


def test_no_key_fallback_is_none(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    d = asyncio.run(decide_action(
        task="t", url="https://a.example", start_url="https://a.example",
        elements=_els(2), focused=FocusedField(), page_text="p", history=[],
    ))
    assert isinstance(d, JevDecision) and d.kind == Kind.NONE and d.confidence == 0.0
    assert d.page_ready == 0.0 and d.needs_text == 0.0 and d.task_done == 0.0


def test_heal_strategies_cover_criteria():
    assert set(HEAL_CRITERIA) == {s.value for s in HealStrategy}
    assert len(HealStrategy) == 6


def test_ref_to_idx_maps_refs_and_bare_ints():
    els = _els(3)
    assert ref_to_idx("e2", els) == 2
    assert ref_to_idx("2", els) == 2
    assert ref_to_idx("e9", els) == -1
    assert ref_to_idx("bogus", els) == -1
    assert ref_to_idx("", els) == -1


def test_heal_triage_no_key_falls_back_to_remap(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    strat, need = asyncio.run(decide_heal_action(
        error_msg="error: element #2 stale map", last_kind="click_item",
        page_text="p"))
    assert strat == HealStrategy.REMAP_STALE and need == 0.0


def test_heal_triage_decodes_strategy_and_clamps_noul(monkeypatch):
    import src.decide as _decide

    async def _fake_post(payload, timeout_s, base, key):
        assert "strategy" in payload["questions"]
        return {"answers": {"strategy": {"choice": "dismiss_cover"},
                            "need_new_cap": {"noul": 9.9}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    strat, need = asyncio.run(decide_heal_action(
        error_msg="error: covered", last_kind="click_item", page_text="p"))
    assert strat == HealStrategy.DISMISS_COVER and need == 1.0


def test_heal_triage_sends_target_kind(monkeypatch):
    import src.decide as _decide

    seen = {}

    async def _fake_post(payload, timeout_s, base, key):
        seen.update(payload["state"])
        return {"answers": {"strategy": {"choice": "remap_stale"},
                            "need_new_cap": {"noul": 0.0}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    strat, _ = asyncio.run(decide_heal_action(
        error_msg="error: no bounding box", last_kind="click_item",
        page_text="p", target_kind="link"))
    assert strat == HealStrategy.REMAP_STALE
    assert seen["target_kind"] == "link"
    assert "no bounding box" in seen["error"]


def test_post_uses_official_sdk_transport(monkeypatch):
    """_post speaks System One through typesafe_sdk (typed questions),
    returning the same raw-answers shape the decoders read."""
    import sys
    import types

    import src.decide as _decide

    seen = {}

    class ChoiceAnswer:
        def __init__(self):
            self.choice = "click_item"
            self.confidence = 0.9
            self.probabilities = {"click_item": 0.9}

    class NoulAnswer:
        def __init__(self):
            self.noul = 0.7

    class ScoreAnswer:
        def __init__(self):
            self.score = 0.4
            self.confidence = 0.5

    class FakeResp:
        model = "jev-1.13.0"
        answers = {"kind": ChoiceAnswer(), "ready": NoulAnswer(),
                   "progress": ScoreAnswer()}

    class FakeClient:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        async def system_one(self, **kwargs):
            seen["call"] = kwargs
            return FakeResp()

        async def aclose(self):
            seen["closed"] = True

    fake_sdk = types.ModuleType("typesafe_sdk")
    fake_sdk.AsyncTypeSafeClient = FakeClient
    fake_sdk.Choice = lambda **k: ("Choice", k)
    fake_sdk.Noul = lambda **k: ("Noul", k)
    fake_sdk.Score = lambda **k: ("Score", k)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", fake_sdk)
    payload = {"model": "jev-latest", "state": {"url": "u"},
               "questions": {
                   "kind": {"type": "choice", "instructions": {"question": "q"},
                            "criteria": {"a": "b"}},
                   "ready": {"type": "noul", "instructions": {"question": "q2"}},
                   "progress": {"type": "score", "instructions": {},
                                "criteria": []},
                   "bogus": {"type": "unknown"},
               }}
    out = asyncio.run(_decide._post(payload, 30.0, "https://x.test/v1", "k"))
    assert out["answers"]["kind"] == {"choice": "click_item",
                                      "confidence": 0.9,
                                      "probabilities": {"click_item": 0.9}}
    assert out["answers"]["ready"] == {"noul": 0.7}
    assert out["answers"]["progress"] == {"score": 0.4, "confidence": 0.5}
    assert out["model"] == "jev-1.13.0"
    assert seen["init"]["api_key"] == "k"
    assert seen["call"]["model"] == "jev-latest"
    assert seen["call"]["timeout"] == 30.0
    kinds = {qid: marker[0] for qid, marker in
             seen["call"]["questions"].items()}
    assert kinds == {"kind": "Choice", "ready": "Noul",
                     "progress": "Score"}  # bogus dropped
    assert seen["closed"] is True


def test_heal_triage_invalid_choice_falls_back(monkeypatch):
    import src.decide as _decide

    async def _fake_post(payload, timeout_s, base, key):
        return {"answers": {"strategy": {"choice": "teleport"},
                            "need_new_cap": {"noul": "junk"}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    strat, need = asyncio.run(decide_heal_action(
        error_msg="e", last_kind="click_item", page_text="p"))
    assert strat == HealStrategy.REMAP_STALE and need == 0.0


def test_recovery_triage_no_key_abstains(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    strat, conf = asyncio.run(decide_recovery_action(
        reason="unknown", act="none", result="", page_excerpt="p"))
    assert (strat, conf) == ("none", 0.0)


def test_recovery_triage_decodes_choice_and_clamps(monkeypatch):
    import src.decide as _decide

    async def _fake_post(payload, timeout_s, base, key):
        assert "recovery" in payload["questions"]
        return {"answers": {"recovery": {"choice": "escape",
                                         "confidence": 9.9}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    strat, conf = asyncio.run(decide_recovery_action(
        reason="covered", act="x", result="covered:div", page_excerpt="p"))
    assert (strat, conf) == ("escape", 1.0)


def test_recovery_triage_invalid_choice_abstains(monkeypatch):
    import src.decide as _decide

    async def _fake_post(payload, timeout_s, base, key):
        return {"answers": {"recovery": {"choice": "teleport"}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert asyncio.run(decide_recovery_action(
        reason="unknown", act="x", result="", page_excerpt="p")) == ("none", 0.0)


def test_restart_action_no_key_defaults_back(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert asyncio.run(decide_restart_action(
        candidates=["https://a.example/"], current_url="https://b.example/",
        streak=3)) == ("back", None, 0.0)


def test_restart_action_decodes_goto_and_clamps(monkeypatch):
    import src.decide as _decide

    async def _fake_post(payload, timeout_s, base, key):
        crit = payload["questions"]["restart"]["criteria"]
        assert "back" in crit and crit["0"].startswith("https://a.example")
        return {"answers": {"restart": {"choice": "0", "confidence": 9.9}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert asyncio.run(decide_restart_action(
        candidates=["https://a.example/"], current_url="https://b.example/",
        streak=6)) == ("goto", "https://a.example/", 1.0)


def test_restart_action_invalid_choice_defaults_back(monkeypatch):
    import src.decide as _decide

    async def _fake_post(payload, timeout_s, base, key):
        return {"answers": {"restart": {"choice": "7"}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert asyncio.run(decide_restart_action(
        candidates=["https://a.example/"], current_url="https://b.example/",
        streak=3)) == ("back", None, 0.0)
