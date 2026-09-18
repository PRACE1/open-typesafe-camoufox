"""crawl_extract.py — crawl4ai as a pure extraction LIBRARY (no crawler).

Deliberate boundary: crawl4ai's ``AsyncWebCrawler`` (its own Chromium,
its own automation) is NEVER used here — Camoufox is the only browser.
What we use are the LLM-free extraction strategies
(``JsonCssExtractionStrategy`` / ``RegexExtractionStrategy``) called
directly via ``strategy.extract(url, html)`` on our live DOM HTML
(``page.content()``). No network, no second browser, no model calls:
CSS/XPath selectors and pre-compiled regexes do exactly what the
schema says — fast, repeatable, carbon-free.

Two outputs, both plain JSON:
- ``extract_page_json``: ordered content blocks + links (+entities).
- ``chunk_blocks``: same ``{url,section,idx,text}`` chunk shape as
  ``jina_client.chunk_markdown`` so the recall pipeline treats Reader
  JSON and DOM JSON identically.

All entry points fail-soft ({} / []) — the caller keeps excerpt
banking. Heavy ``crawl4ai`` imports stay function-local so offline
runs and collection tools never pay the import cost.
"""

from __future__ import annotations

from typing import Any

# Container cascade: most-specific article landmark first, body last.
BASE_CANDIDATES = ("main", "article", "[role='main']", "body")

# NOTE: ``_extract_with`` stamps the container (main/article/.../body)
# onto ``baseSelector`` — so these schemas define fields RELATIVE to the
# container, never their own base. Standalone tag-list schemas would be
# silently overwritten (and lose container scoping to nav/footer).
BLOCKS_SCHEMA = {
    "name": "ContentBlocks",
    "fields": [{
        "name": "blocks",
        "selector": "h1, h2, h3, p, li, blockquote, pre, td",
        "type": "list",
        "fields": [{"name": "text", "type": "text"}],
    }],
}

LINKS_SCHEMA = {
    "name": "PageLinks",
    "fields": [{
        "name": "links",
        "selector": "a[href]",
        "type": "nested_list",
        "fields": [
            {"name": "text", "type": "text", "default": ""},
            {"name": "href", "type": "attribute", "attribute": "href",
             "default": ""},
        ],
    }],
}

TITLE_SCHEMA = {
    "name": "PageTitle",
    "fields": [{"name": "text", "selector": "h1", "type": "text",
                "default": ""}],
}

DOCTITLE_SCHEMA = {
    "name": "DocTitle",
    "fields": [{"name": "text", "selector": "title", "type": "text",
                "default": ""}],
}

MAX_HTML_CHARS = 2_000_000
MAX_BLOCKS = 500
MAX_LINKS = 200
MAX_TEXT_CHARS = 300


def _truncate_text(text: Any) -> str:
    words = " ".join(str(text or "").split())
    if len(words) > MAX_TEXT_CHARS:
        words = words[:MAX_TEXT_CHARS].rstrip() + "..."
    return words


def _extract_with(schema: dict[str, Any], base: str, url: str,
                  html: str) -> list[dict[str, Any]]:
    """Run one CSS schema against raw HTML (library-only, no crawler)."""
    from crawl4ai import JsonCssExtractionStrategy

    local = dict(schema)
    local["baseSelector"] = base
    try:
        out = JsonCssExtractionStrategy(
            local, verbose=False).extract(url, html)
    except Exception:  # noqa: BLE001
        return []
    return [o for o in (out or []) if isinstance(o, dict)]


def extract_page_json(url: str, html: str) -> dict[str, Any]:
    """Live DOM HTML → structured JSON (no browser, no network, no LLM).

    Returns ``{url, title, blocks[{text}], links[{text,href}],
    entities[{label,value}]}`` in document order. Title prefers the
    first h1, then <title>. Empty dict when nothing extracts.
    """
    url = str(url or "")
    html = str(html or "")
    if not html.strip():
        return {}
    if len(html) > MAX_HTML_CHARS:
        html = html[:MAX_HTML_CHARS]
    def _first_text(rows: list[dict[str, Any]]) -> str:
        for row in rows:
            if str(row.get("text") or "").strip():
                return _truncate_text(row["text"])
        return ""

    try:
        title = ""
        for base in BASE_CANDIDATES:
            title = _first_text(_extract_with(TITLE_SCHEMA, base, url, html))
            if title:
                break
        if not title:
            for base in BASE_CANDIDATES:
                title = _first_text(
                    _extract_with(DOCTITLE_SCHEMA, base, url, html))
                if title:
                    break
        blocks: list[dict[str, str]] = []
        for base in BASE_CANDIDATES:
            rows = _extract_with(BLOCKS_SCHEMA, base, url, html)
            items = (rows[0].get("blocks", []) if rows else [])
            texts = [_truncate_text(i.get("text")) for i in items
                     if isinstance(i, dict)]
            texts = [t for t in texts if t]
            if texts:
                blocks = [{"text": t} for t in texts[:MAX_BLOCKS]]
                break
        links: list[dict[str, str]] = []
        for base in BASE_CANDIDATES:
            rows = _extract_with(LINKS_SCHEMA, base, url, html)
            items = (rows[0].get("links", []) if rows else [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = _truncate_text(item.get("text"))
                href = str(item.get("href") or "").strip()[:300]
                if href:
                    links.append({"text": text, "href": href})
                    if len(links) >= MAX_LINKS:
                        break
            if links:
                break
        entities = extract_entities(url, html)
        doc: dict[str, Any] = {"url": url, "title": title, "blocks": blocks,
                               "links": links, "entities": entities}
        if not blocks and not links:
            return {}
        return doc
    except Exception:  # noqa: BLE001
        return {}


def extract_entities(url: str,
                     html: str) -> list[dict[str, str]]:
    """Fast regex entities (emails, phones, urls) — zero LLM, zero net."""
    try:
        from crawl4ai import RegexExtractionStrategy
    except Exception:  # noqa: BLE001
        return []
    try:
        strategy = RegexExtractionStrategy(
            pattern=(RegexExtractionStrategy.Email
                     | RegexExtractionStrategy.PhoneIntl
                     | RegexExtractionStrategy.Url))
        out = strategy.extract(str(url or ""), str(html or "")[:500_000])
    except Exception:  # noqa: BLE001
        return []
    cleaned: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in out or []:
        if not isinstance(item, dict):
            continue
        key = (str(item.get("label") or ""), str(item.get("value") or ""))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        cleaned.append({"label": key[0], "value": key[1][:300]})
        if len(cleaned) >= 50:
            break
    return cleaned


def chunk_blocks(url: str, page_json: dict[str, Any],
                 target_chars: int = 700,
                 max_chunks: int = 64) -> list[dict[str, Any]]:
    """Structured blocks → recall chunks ``{url,section,idx,text}``.

    Same shape as ``jina_client.chunk_markdown`` output: short leading
    blocks become the section name, the rest accumulate to target size.
    Pure function.
    """
    blocks = [b for b in (page_json or {}).get("blocks", [])
              if isinstance(b, dict) and str(b.get("text") or "").strip()]
    if not blocks:
        return []
    title = str((page_json or {}).get("title") or "")[:80]
    chunks: list[dict[str, Any]] = []
    section = title or "top"
    buf = ""
    for block in blocks:
        text = " ".join(str(block.get("text") or "").split())
        if not text:
            continue
        if not buf and len(text) <= 120 and not text.endswith((".", "!", "?", ":")):
            section = text[:80]  # heading-like lead block names the chunk
            continue
        if buf and len(buf) + len(text) > target_chars * 2:
            chunks.append({"url": url, "section": section,
                           "idx": len(chunks), "text": buf})
            buf = ""
            section = title or "top"
        buf = f"{buf}\n{text}" if buf else text
        if len(chunks) >= max_chunks:
            break
    if buf.strip() and len(chunks) < max_chunks:
        chunks.append({"url": url, "section": section,
                       "idx": len(chunks), "text": buf})
    return chunks[:max_chunks]


def doc_to_markdown(page_json: dict[str, Any]) -> dict[str, Any]:
    """Structured page JSON → Reader-shaped ``{url,title,content}`` doc.

    Lets DOM extractions flow through the identical chunk/rerank/store
    path as Reader docs (``source: dom`` distinguishes them).
    """
    blocks = (page_json or {}).get("blocks", []) or []
    lines = [str(b.get("text") or "") for b in blocks
             if isinstance(b, dict)]
    return {"url": str((page_json or {}).get("url") or ""),
            "title": str((page_json or {}).get("title") or ""),
            "content": "\n".join(lines), "timestamp": ""}
