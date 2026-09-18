"""Kraken client tests — hosted vision contract (offline, stubbed httpx).

No key, no network: the transport is faked at the httpx boundary and
env is monkeypatched. Secrets must never appear in logs or records.
"""

import asyncio
import json

from src.capability.backends import kraken as kk


def _run(coro):
    return asyncio.run(coro)


class _FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeClient:
    seen = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.seen.append({"url": url, "json": json,
                                 "headers": dict(headers or {})})
        content = ("Pick tiles [2, 8, 9] for traffic lights "
                   "grid 3x3 response")
        return _FakeResponse(200, {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 348, "completion_tokens": 10}})


def _patch(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.seen = []
    monkeypatch.setenv("CAPTCHA_KRAKEN_API_KEY", "ck_live_test")
    monkeypatch.setenv("VLLM_BASE_URL", "https://api.captchakraken.com/v1")


def test_model_selection_hosted_vs_selfhost(monkeypatch):
    monkeypatch.setenv("VLLM_BASE_URL", "https://api.captchakraken.com/v1")
    assert kk.model_for_grid() == "abyss-grid"
    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:8000/v1")
    assert kk.model_for_grid() == "captcha-v12"


def test_parse_indexes_one_based_to_zero_based():
    assert kk.parse_indexes("[2, 8, 9]", 9) == [1, 7, 8]
    assert kk.parse_indexes("tiles [1, 1, 99, 0, x]", 9) == [0]
    assert kk.parse_indexes("no brackets here", 9) == []
    assert kk.parse_indexes(None, 9) == []


def test_grid_prompt_names_dims_and_instruction():
    prompt = kk.grid_prompt("Select all images with bicycles", 3, 3)
    assert "3x3" in prompt and "bicycles" in prompt
    assert "JSON Array" in prompt


def test_solve_grid_roundtrip_and_masking(monkeypatch):
    _patch(monkeypatch)
    idx, usage, logs = _run(kk.solve_grid(
        b"fake-png", "traffic lights", 3, 3, session_id="sess-1"))
    assert idx == [1, 7, 8]
    assert usage["model"] == "abyss-grid"
    call = _FakeClient.seen[0]
    assert call["url"].endswith("/chat/completions")
    assert call["headers"]["Authorization"] == "Bearer ck_live_test"
    assert call["headers"]["X-CK-Session"] == "sess-1"
    assert call["json"]["model"] == "abyss-grid"
    blob = json.dumps({"logs": logs, "usage": usage})
    assert "ck_live_test" not in blob  # key never in logs/records


def test_solve_grid_refuses_without_key(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.delenv("CAPTCHA_KRAKEN_API_KEY")
    idx, usage, logs = _run(kk.solve_grid(b"png", "t", 3, 3))
    assert idx == [] and usage["code"] == "missing_api_key"
    assert _FakeClient.seen == []  # no network attempted


def test_error_codes_branch(monkeypatch):
    _patch(monkeypatch)

    async def _post_429(self, url, json=None, headers=None):
        return _FakeResponse(429, {"ck_error": {
            "code": "rate_limited", "retry_after_seconds": 7}})

    import httpx

    class _Limited(_FakeClient):
        async def post(self, url, json=None, headers=None):
            return await _post_429(self, url, json, headers)

    monkeypatch.setattr(httpx, "AsyncClient", _Limited)
    idx, usage, logs = _run(kk.solve_grid(b"png", "t", 3, 3))
    assert idx == [] and usage["code"] == "rate_limited"
    assert usage["retry_after_seconds"] == 7

    class _Denied(_FakeClient):
        async def post(self, url, json=None, headers=None):
            return _FakeResponse(401, {"ck_error": {"code": "invalid_api_key"}})

    monkeypatch.setattr(httpx, "AsyncClient", _Denied)
    idx, usage, logs = _run(kk.solve_grid(b"png", "t", 3, 3))
    assert usage["code"] == "invalid_api_key"


def test_session_ids_unique_per_captcha():
    assert kk.new_session_id() != kk.new_session_id()
