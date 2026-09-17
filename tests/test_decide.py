"""Decide tests — mutual exclusion, caps, decode, no-key fallback (all offline)."""

import asyncio

from src.decide import (
    Kind, KIND_CRITERIA, MAX_ITEMS, JevDecision,
    build_questions, decide_action,
)
from src.deps import ElementRef, FocusedField


def _els(n):
    return [ElementRef(idx=i, kind="link", label=f"l{i}", cx=0.1, cy=0.1) for i in range(n)]


def test_kinds_mutually_exclusive():
    assert len(KIND_CRITERIA) == len({k.value for k in Kind}) == 11
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
                      "task_done", "progress"}
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
    els = [_E(idx=0, kind="input", type="text", label="Search",
              text="", href="", cx=0.5, cy=0.4, value="q", value_len=1)]
    q = build_questions(els, ["https://a.example"],
                        visited=["https://other.example/"])
    opt = q["item"]["criteria"]["0"]
    assert opt["label"] == "Search" and opt["state"] == "filled(1ch)"
    assert opt["sel"] == '[data-jev="0"]' and opt["visited"] is False


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


def test_noul_score_defaults_when_missing():
    d = _decode(_raw("wait"), n=1)
    assert d.page_ready == 0.0 and d.needs_text == 0.0
    assert d.task_done == 0.0 and d.progress == 0.0


def _decode(raw, n=3):
    from src.decide import _decode
    sites = ["https://a.example", "https://b.example"]
    return _decode(raw, _els(n), sites)


def test_decode_click_item():
    d = _decode(_raw("click_item", item="2"))
    assert d.kind == Kind.CLICK_ITEM and d.element_idx == 2 and d.confidence == 0.9
    assert d.need_text is False


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
