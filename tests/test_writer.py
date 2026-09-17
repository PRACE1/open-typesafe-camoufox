"""Writer tests — URL validation, credential gate (offline, no network)."""

import asyncio

import src.writer as _w
from src.deps import ElementRef
from src.writer import (
    ProposedAction, TaskVerdict, WriterText, WriterUrl, compose_text,
    propose_action, propose_url, summarize_elements, summarize_task,
    validate_url,
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


def test_summarize_task_done_and_refusals(monkeypatch):
    notes = ["https://a.example/ :: Europa has a salty ocean",
             "https://b.example/ :: Enceladus vents water"]
    ok = _fake_chat({"done": True, "note": "Europa: salty ocean; Enceladus: vents"})
    monkeypatch.setattr(_w, "_chat_json", ok)
    v = asyncio.run(summarize_task(task="find water facts", notes=notes))
    assert isinstance(v, TaskVerdict) and v.done is True and "Europa" in v.note
    empty = _fake_chat({"done": True, "note": "   "})
    monkeypatch.setattr(_w, "_chat_json", empty)
    assert asyncio.run(summarize_task(task="t", notes=notes)).done is False
    no = _fake_chat({"done": False, "note": "not yet"})
    monkeypatch.setattr(_w, "_chat_json", no)
    assert asyncio.run(summarize_task(task="t", notes=notes)).done is False
    bad = _fake_chat("not json shaped")
    monkeypatch.setattr(_w, "_chat_json", bad)
    assert asyncio.run(summarize_task(task="t", notes=notes)).done is False


def test_chat_json_reports_provider_http_status(monkeypatch, capsys):
    import httpx as _httpx

    class _Resp:
        status_code = 403
        text = '{"error":{"message":"Access denied."}}'

        def raise_for_status(self):
            raise _httpx.HTTPStatusError("403", request=None, response=None)

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    try:
        asyncio.run(_w._chat_json("system", "user"))
        raised = False
    except _httpx.HTTPStatusError:
        raised = True
    assert raised is True
    assert "writer model HTTP 403" in capsys.readouterr().out
