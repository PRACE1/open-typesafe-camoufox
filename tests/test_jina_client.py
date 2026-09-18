"""Jina client tests — fetch/chunk/rerank shapes (offline, stubbed httpx)."""

import asyncio
import json

from src.capability import jina_client as jc


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
    """Stub httpx.AsyncClient: routes by URL, records headers (masked)."""
    seen = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _FakeClient.seen.append(("GET", url, headers))
        return _FakeResponse(200, {"url": "https://a.example/",
                                   "title": "Example",
                                   "content": "# Hi\n\nBody text here.",
                                   "timestamp": "2026-01-01"})

    async def post(self, url, json=None, headers=None):
        _FakeClient.seen.append(("POST", url, headers))
        docs = (json or {}).get("documents", [])
        top_n = (json or {}).get("top_n", 5)
        results = [{"index": i, "relevance_score": 0.9 - i * 0.1}
                   for i in range(min(top_n, len(docs)))]
        return _FakeResponse(200, {"model": jc.RERANK_MODEL,
                                   "usage": {"total_tokens": 10},
                                   "results": results})


def _patch(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _FakeClient.seen = []


def test_fetch_page_returns_structured_doc(monkeypatch):
    _patch(monkeypatch)
    doc = _run(jc.fetch_page("https://a.example/"))
    assert doc["title"] == "Example" and "Body text" in doc["content"]
    method, url, headers = _FakeClient.seen[0]
    assert method == "GET" and headers["Accept"] == "application/json"
    assert headers["X-Respond-With"] == "markdown"


def test_fetch_page_empty_url_never_networks(monkeypatch):
    _patch(monkeypatch)
    assert _run(jc.fetch_page(""))["error"] == "empty url"
    assert _FakeClient.seen == []


def test_fetch_transport_failure_is_structured(monkeypatch):
    import httpx

    class _Boom(_FakeClient):
        async def get(self, url, headers=None):
            raise RuntimeError("dns boom")

    monkeypatch.setattr(httpx, "AsyncClient", _Boom)
    doc = _run(jc.fetch_page("https://a.example/"))
    assert "transport failed" in doc["error"]


def test_chunk_splits_on_headings():
    doc = {"url": "https://a.example/", "title": "T",
           "content": "# Alpha\n\n" + ("x" * 200) + "\n\n# Beta\n\n"
                      + ("y" * 200)}
    chunks = jc.chunk_markdown(doc)
    assert len(chunks) == 2
    assert chunks[0]["section"] == "Alpha" and chunks[1]["idx"] == 1
    assert all(c["url"] == "https://a.example/" for c in chunks)


def test_chunk_drops_scraps_and_caps():
    doc = {"url": "u", "content": "tiny"}
    assert jc.chunk_markdown(doc) == []
    big = {"url": "u", "content": "".join(
        f"# S{i}\n\n{'z' * 200}\n\n" for i in range(80))}
    assert len(jc.chunk_markdown(big)) == jc.MAX_CHUNKS


def test_rerank_maps_scores_to_indexes(monkeypatch):
    _patch(monkeypatch)
    ranked, usage = _run(jc.rerank("query", ["d0", "d1", "d2"], top_n=2))
    assert ranked == [{"index": 0, "score": 0.9},
                      {"index": 1, "score": 0.8}]
    assert usage == {"total_tokens": 10}
    method, url, headers = _FakeClient.seen[0]
    assert method == "POST" and url == jc.RERANK_URL
    assert headers["Accept"] == "application/json"


def test_rerank_auth_header_only_with_key(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    _run(jc.rerank("q", ["d"]))
    assert "Authorization" not in _FakeClient.seen[0][2]
    monkeypatch.setenv("JINA_API_KEY", "jina_secret")
    _run(jc.rerank("q", ["d"]))
    headers = _FakeClient.seen[-1][2]
    assert headers["Authorization"] == "Bearer jina_secret"


def test_rerank_empty_inputs_never_network(monkeypatch):
    _patch(monkeypatch)
    assert _run(jc.rerank("", ["d"]))[0] == []
    assert _run(jc.rerank("q", []))[0] == []
    assert _FakeClient.seen == []


def test_rerank_rate_limit_backs_off(monkeypatch):
    import httpx

    class _Limited(_FakeClient):
        async def post(self, url, json=None, headers=None):
            return _FakeResponse(429, {})

    monkeypatch.setattr(httpx, "AsyncClient", _Limited)
    ranked, meta = _run(jc.rerank("q", ["d"]))
    assert ranked == [] and "rate-limited" in meta["error"]
