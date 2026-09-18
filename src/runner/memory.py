"""runner.memory — scored page memory: fetch → chunk → rerank → recall.

Replaces blind excerpt banking with evidence: a newly settled page is
fetched as Reader JSON, split into section chunks, stored in
``memory.jsonl``, scored against the task query by the reranker, and
the winners return as note strings JEV already reasons over (no
``decide.py`` changes — recalled lines ride the existing notes buffer).

Everything fail-softs to the old path: any transport/API failure
yields [] and the caller keeps excerpt banking. Offline runs behave
exactly as before.
"""

from __future__ import annotations

from typing import Any

RECALL_TOP_N = 3
RECALL_LINE_CHARS = 300


def format_recalled(url: str, section: str, score: float,
                    text: str) -> str:
    """One recalled chunk as a JEV-readable note line."""
    snippet = " ".join(str(text or "").split())
    if len(snippet) > RECALL_LINE_CHARS:
        snippet = snippet[:RECALL_LINE_CHARS].rstrip() + "..."
    return (f"recall({url} {section or 'top'} {score:.2f}): {snippet}")


async def recall_page(*, run_dir: str, report_mod: Any, url: str,
                      task_text: str, context_note: str = "",
                      top_n: int = RECALL_TOP_N,
                      page_html: str = "") -> list[str]:
    """Full recall pass for one newly settled page → note lines.

    Reader JSON first; when it fails (authed/JS-walled pages Reader
    can't see), the live DOM HTML goes through crawl4ai's library-only
    CSS/regex strategies (no crawler, no second browser — Camoufox is
    the only browser) into the same chunk shape. Then: store rows →
    rerank vs ``task + context`` → format winners. Never raises; []
    means "keep excerpts".
    """
    try:
        from ..capability import jina_client as _jina

        source = "reader"
        doc = await _jina.fetch_page(url)
        if not isinstance(doc, dict) or doc.get("error") or not str(
                doc.get("content") or "").strip():
            doc = None
            if (page_html or "").strip():
                from ..capability import crawl_extract as _ce

                page_json = _ce.extract_page_json(url, page_html)
                if page_json.get("blocks"):
                    chunks = _ce.chunk_blocks(url, page_json)
                    source = "dom"
                else:
                    chunks = []
            else:
                chunks = []
        else:
            chunks = _jina.chunk_markdown(doc)
        if not chunks:
            return []
        rows = [{"url": c["url"], "section": c["section"], "idx": c["idx"],
                 "text": c["text"], "score": 0.0, "source": source}
                for c in chunks]
        report_mod.write_memory_jsonl(run_dir, rows)
        query = task_text
        if (context_note or "").strip():
            query = f"{task_text}\n{context_note.strip()[:300]}"
        ranked, _usage = await _jina.rerank(
            query, [c["text"] for c in chunks], top_n=top_n)
        recalled: list[str] = []
        by_index = {c["idx"]: c for c in chunks}
        for item in ranked or []:
            chunk = by_index.get(item.get("index"))
            if chunk is None:
                continue
            recalled.append(format_recalled(
                chunk["url"], chunk["section"],
                float(item.get("score", 0.0)), chunk["text"]))
        return recalled
    except Exception:  # noqa: BLE001
        return []
