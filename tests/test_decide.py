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
    assert len(KIND_CRITERIA) == len({k.value for k in Kind}) == 7
    assert set(KIND_CRITERIA) == {k.value for k in Kind}


def test_item_choice_caps_at_255():
    q = build_questions(_els(300), ["https://a.example"])
    assert len(q["item"]["criteria"]) == MAX_ITEMS == 255


def test_item_choice_empty_page_has_sentinel():
    q = build_questions([], ["https://a.example"])
    assert q["item"]["criteria"] == {"-1": "No actionable elements on this page"}


def test_site_choice_has_other():
    q = build_questions(_els(2), ["https://a.example", "https://b.example"])
    assert q["site"]["criteria"]["other"].startswith("A different URL")


def _raw(kind, item="1", site="0", conf=0.9):
    return {"answers": {
        "kind": {"choice": kind, "confidence": conf},
        "item": {"choice": item},
        "site": {"choice": site},
    }}


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
