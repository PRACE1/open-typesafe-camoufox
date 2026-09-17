"""Writer tests — URL validation, credential gate (offline, no network)."""

import asyncio

import src.writer as _w
from src.deps import ElementRef
from src.writer import (
    ProposedAction, WriterText, WriterUrl, compose_text, propose_action,
    propose_url, summarize_elements, validate_url,
)


def test_validate_url_accepts_clean_https():
    assert validate_url("https://www.google.com/search?q=moon") == "https://www.google.com/search?q=moon"


def test_validate_url_rejects():
    assert validate_url(None) is None
    assert validate_url("") is None
    assert validate_url("http://example.com") is None
    assert validate_url("javascript:alert(1)") is None
    assert validate_url("data:text/html,hi") is None
    assert validate_url("https://") is None
    assert validate_url("https://user:pass@example.com") is None
    assert validate_url("not a url") is None


def test_compose_text_credential_never_calls_model():
    w = asyncio.run(compose_text(
        task="sign in", field_label="Password", placeholder="", nearby_text="",
        history=[], is_credential=True,
    ))
    assert isinstance(w, WriterText) and w.fill is False and w.text == ""


def test_compose_text_without_key_declines(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    w = asyncio.run(compose_text(
        task="search", field_label="Search", placeholder="", nearby_text="",
        history=[], is_credential=False,
    ))
    assert w.fill is False


def test_propose_url_without_key_not_ok(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    w = asyncio.run(propose_url(task="go somewhere", history=[]))
    assert isinstance(w, WriterUrl) and w.ok is False


def _els():
    return [ElementRef(idx=0, kind="link", text="Prolific",
                       href="https://www.prolific.com/", region="main",
                       cx=0.3, cy=0.4),
            ElementRef(idx=5, kind="textarea", label="Search", cx=0.5, cy=0.4)]


def _fake_chat(payload):
    async def _fake(system, user, timeout_s=30.0):
        _fake.calls.append((system, user))
        return payload
    _fake.calls = []
    return _fake


def test_summarize_elements_shows_urls():
    s = summarize_elements(_els())
    assert "[0] link" in s and "prolific.com" in s and "[main]" in s
    assert "[5] textarea" in s and "empty" in s
    assert "LINKS:" in s and "INPUTS:" in s


def test_propose_valid_click(monkeypatch):
    fake = _fake_chat({"question": "Should the browser click the Prolific link?",
                       "kind": "click_item", "item": 0, "url": None,
                       "rationale": "Top result matches the task."})
    monkeypatch.setattr(_w, "_chat_json", fake)
    p = asyncio.run(propose_action(task="t", url="https://g.example/", elements=_els(),
                                   page_text="p", history=[], notes=[]))
    assert isinstance(p, ProposedAction)
    assert p.kind == "click_item" and p.item == 0 and p.url is None
    assert "Prolific" in p.question


def test_propose_rejects_bad_kind_item_url(monkeypatch):
    bad_kind = _fake_chat({"question": "q?", "kind": "teleport", "item": 0,
                           "url": None, "rationale": "r"})
    monkeypatch.setattr(_w, "_chat_json", bad_kind)
    assert asyncio.run(propose_action(task="t", url="u", elements=_els(),
                                      page_text="p", history=[], notes=[])) is None
    bad_item = _fake_chat({"question": "q?", "kind": "click_item", "item": -2,
                           "url": None, "rationale": "r"})
    monkeypatch.setattr(_w, "_chat_json", bad_item)
    assert asyncio.run(propose_action(task="t", url="u", elements=_els(),
                                      page_text="p", history=[], notes=[])) is None
    bad_url = _fake_chat({"question": "q?", "kind": "goto", "item": None,
                          "url": "javascript:alert(1)", "rationale": "r"})
    monkeypatch.setattr(_w, "_chat_json", bad_url)
    assert asyncio.run(propose_action(task="t", url="u", elements=_els(),
                                      page_text="p", history=[], notes=[])) is None


def test_propose_no_elements_no_call(monkeypatch):
    fake = _fake_chat({"question": "q?", "kind": "wait", "item": None,
                       "url": None, "rationale": "r"})
    monkeypatch.setattr(_w, "_chat_json", fake)
    assert asyncio.run(propose_action(task="t", url="u", elements=[],
                                      page_text="p", history=[], notes=[])) is None
    assert fake.calls == []


def test_propose_reading_note_reaches_prompt(monkeypatch):
    fake = _fake_chat({"question": "Should the browser wait?",
                       "kind": "wait", "item": None, "url": None,
                       "rationale": "Reading."})
    monkeypatch.setattr(_w, "_chat_json", fake)
    p = asyncio.run(propose_action(task="t", url="u", elements=_els(),
                                   page_text="p", history=[], notes=[],
                                   reading_note="CURRENTLY READING: https://a.example/"))
    assert isinstance(p, ProposedAction) and p.kind == "wait"
    assert "CURRENTLY READING" in fake.calls[0][1]
