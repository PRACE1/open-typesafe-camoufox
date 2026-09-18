"""jina_client.py — Jina Reader fetch + chunk + rerank (optional backends).

Contracts (verified 2026-09-18 against the live docs):
- Reader: ``GET https://r.jina.ai/{url}`` (or POST ``{url}``) with
  ``Accept: application/json`` → ``{url, title, content, timestamp}``.
  Shaping headers: ``X-Respond-With: markdown`` (default pipeline),
  ``X-Timeout``, ``X-Retain-Links/Images``, ``X-No-Cache``.
  Anonymous works (20 RPM); ``Authorization: Bearer $JINA_API_KEY``
  raises the ceiling.
- Reranker: ``POST https://api.jina.ai/v1/rerank`` with
  ``{model: jina-reranker-v3.5, query, documents[], top_n,
  return_documents}`` → ``{model, usage, results: [{index,
  relevance_score}]}``.

Role in the solver: turn a visited page into scored, storable memory.
Fetch returns one structured doc; chunk splits it into <=64 section
docs; rerank scores them against the task query; the loop stores the
winners in ``memory.jsonl`` and recalls the top-k into the state
packet for JEV. Every stage fail-softs to the pre-existing DOM-text
path — offline runs behave exactly as before.

Secrets: ``JINA_API_KEY`` read at call time from ``.env.local``;
masked in all logs (presence only, never the value).
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from .logging_utils import log as _base_log


def log(msg: str, tag: str = "jina") -> None:
    """Hierarchical domain logger (default ``jina[:sub]``)."""
    _base_log(msg, tag=tag)

READER_BASE_URL = "https://r.jina.ai"
RERANK_URL = "https://api.jina.ai/v1/rerank"
RERANK_MODEL = "jina-reranker-v3.5"
MAX_CHUNKS = 64  # v3 listwise window
CHUNK_MIN_CHARS = 120
CHUNK_TARGET_CHARS = 700
REQUEST_TIMEOUT_S = 30.0


def _api_key() -> str:
    return os.environ.get("JINA_API_KEY", "").strip()


def is_available() -> bool:
    """True when the Reader path can be attempted.

    Reader works anonymously (free tier), so availability is about the
    httpx transport, not the key. Rerank calls without a key still
    attempt (and fail-soft) — the loop never blocks on this.
    """
    try:
        import httpx as _httpx  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if extra:
        headers.update(extra)
    return headers


def _masked() -> str:
    return "key set" if _api_key() else "anonymous (20 RPM)"


async def fetch_page(url: str, timeout_s: float = REQUEST_TIMEOUT_S,
                     respond_with: str = "markdown") -> dict[str, Any]:
    """Reader JSON for one URL → ``{url,title,content,timestamp}``.

    Never raises: transport/API failures return ``{"error": ...}`` and
    the caller falls back to live DOM text.
    """
    if not (url or "").strip():
        return {"error": "empty url"}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.get(
                f"{READER_BASE_URL}/{url.strip()}",
                headers=_headers({"X-Respond-With": respond_with,
                                  "X-Timeout": str(int(timeout_s))}),
            )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"reader transport failed: {exc.__class__.__name__}"}
    try:
        body = res.json()
    except ValueError:
        return {"error": f"reader non-JSON (http {res.status_code})"}
    if res.status_code != 200 or not isinstance(body, dict):
        return {"error": f"reader http {res.status_code}: "
                         f"{str(body)[:160]}"}
    doc = {"url": str(body.get("url") or url),
           "title": str(body.get("title") or ""),
           "content": str(body.get("content") or body.get("text") or ""),
           "timestamp": str(body.get("timestamp") or "")}
    if not doc["content"].strip():
        return {"error": "reader returned no content", **doc}
    return doc


def chunk_markdown(doc: dict[str, Any],
                   target_chars: int = CHUNK_TARGET_CHARS,
                   max_chunks: int = MAX_CHUNKS) -> list[dict[str, Any]]:
    """Split a Reader doc into section chunks for reranking.

    Headings open new sections; short scraps merge forward; over-long
    sections split on paragraph boundaries. Pure function — no network.
    Each chunk: ``{url, section, idx, text}``.
    """
    url = str(doc.get("url") or "")
    content = str(doc.get("content") or "")
    sections: list[dict[str, str]] = []
    current = {"section": doc.get("title") or "top", "buf": []}
    for line in content.splitlines():
        stripped = line.strip()
        heading = re.match(r"^#{1,4}\s+(.*)", stripped)
        if heading:
            text = "\n".join(current["buf"]).strip()
            if len(text) >= CHUNK_MIN_CHARS:
                sections.append({"section": current["section"],
                                 "text": text})
            elif text and sections:
                sections[-1]["text"] += "\n" + text
            current = {"section": heading.group(1)[:80], "buf": []}
        elif stripped:
            current["buf"].append(stripped)
    tail = "\n".join(current["buf"]).strip()
    if len(tail) >= CHUNK_MIN_CHARS:
        sections.append({"section": current["section"], "text": tail})
    elif tail and sections:
        sections[-1]["text"] += "\n" + tail
    chunks: list[dict[str, Any]] = []
    for sec in sections:
        paras = [p for p in sec["text"].split("\n") if p]
        buf = ""
        for para in paras:
            if buf and len(buf) + len(para) > target_chars * 2:
                chunks.append({"url": url, "section": sec["section"],
                               "idx": len(chunks), "text": buf})
                buf = ""
            buf = f"{buf}\n{para}" if buf else para
        if buf.strip():
            chunks.append({"url": url, "section": sec["section"],
                           "idx": len(chunks), "text": buf})
        if len(chunks) >= max_chunks:
            break
    return chunks[:max_chunks]


async def rerank(query: str, documents: list[str], top_n: int = 5,
                 timeout_s: float = REQUEST_TIMEOUT_S
                 ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score documents vs query → ``([(index, score)...], usage)``.

    ``return_documents: false`` — we join scores back locally (cheaper,
    no text echo). Never raises: failures return ([], {"error": ...})
    and the caller keeps excerpt banking.
    """
    docs = [d for d in (documents or []) if str(d or "").strip()]
    if not docs:
        return [], {"error": "no documents"}
    if not (query or "").strip():
        return [], {"error": "empty query"}
    payload = {"model": RERANK_MODEL, "query": query[:2000],
               "documents": [d[:4000] for d in docs], "top_n": top_n,
               "return_documents": False}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            res = await client.post(RERANK_URL, json=payload,
                                    headers=_headers())
    except Exception as exc:  # noqa: BLE001
        return [], {"error": f"rerank transport failed: "
                             f"{exc.__class__.__name__}"}
    if res.status_code == 429:
        log("rerank rate-limited — backing off to excerpts")
        return [], {"error": "rate-limited (429)"}
    if res.status_code in (401, 403):
        log("rerank refused (auth) — check JINA_API_KEY")
        return [], {"error": f"refused (http {res.status_code})"}
    try:
        body = res.json()
    except ValueError:
        return [], {"error": f"rerank non-JSON (http {res.status_code})"}
    if res.status_code != 200 or not isinstance(body, dict):
        return [], {"error": f"rerank http {res.status_code}: "
                             f"{str(body)[:160]}"}
    ranked: list[dict[str, Any]] = []
    for item in body.get("results", []) or []:
        try:
            ranked.append({"index": int(item["index"]),
                           "score": round(float(item["relevance_score"]), 4)})
        except (ValueError, TypeError, KeyError):
            continue
    usage = body.get("usage", {}) if isinstance(body, dict) else {}
    log(f"reranked {len(docs)} docs ({_masked()}): "
        f"top={ranked[0]['score'] if ranked else '-'}")
    return ranked, (usage if isinstance(usage, dict) else {})
