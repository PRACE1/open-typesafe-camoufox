"""Writer tests — URL validation, credential gate (offline, no network)."""

import asyncio

from src.writer import WriterText, WriterUrl, compose_text, propose_url, validate_url


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
