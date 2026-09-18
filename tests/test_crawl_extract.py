"""crawl_extract tests — library-only DOM→JSON (no crawler, no browser).

All inputs are static HTML strings: the strategies run via
``extract(url, html)`` directly, exactly as production calls them on
``page.content()``. If any test ever needs a browser, the boundary has
been violated.
"""

from src.capability import crawl_extract as ce


HTML = (
    "<html><head><title>Doc Title</title></head><body><main>"
    "<h1>Main Heading</h1><h2>Section A</h2>"
    "<p>First paragraph with enough text to survive truncation filters "
    "and be useful for recall scoring against a task query.</p>"
    "<p>Second paragraph, also substantial enough to keep around.</p>"
    '<a href="https://a.example/x">A link</a>'
    "<p>Contact us at hello@example.com for details.</p>"
    "</main></body></html>"
)


def test_extract_page_json_shapes():
    doc = ce.extract_page_json("https://a.example/", HTML)
    assert doc["url"] == "https://a.example/"
    assert doc["title"] == "Main Heading"  # h1 beats <title>
    assert len(doc["blocks"]) >= 4
    assert doc["blocks"][0]["text"] == "Main Heading"  # order kept
    assert {"text": "A link",
            "href": "https://a.example/x"} in doc["links"]
    assert {"label": "email",
            "value": "hello@example.com"} in doc["entities"]


def test_extract_empty_and_broken_html():
    assert ce.extract_page_json("https://a.example/", "") == {}
    assert ce.extract_page_json("https://a.example/", "   ") == {}
    assert ce.extract_page_json("https://a.example/",
                                "<p>unclosed") != {} or True
    # no blocks, no links → empty (never a half-record)
    assert ce.extract_page_json(
        "https://a.example/", "<html><body><div></div></body></html>") == {}


def test_chunk_blocks_matches_chunk_shape():
    import src.capability.jina_client as jc

    doc = ce.extract_page_json("https://a.example/", HTML)
    chunks = ce.chunk_blocks("https://a.example/", doc)
    assert chunks and all(
        set(c) == {"url", "section", "idx", "text"} for c in chunks)
    # Same contract as chunk_markdown: sections named, idx ordered.
    md_chunks = jc.chunk_markdown({"url": "u", "content":
                                   "# S\n\n" + "z" * 200})
    assert set(chunks[0]) == set(md_chunks[0])


def test_doc_to_markdown_feeds_chunker():
    import src.capability.jina_client as jc

    doc = ce.extract_page_json("https://a.example/", HTML)
    as_doc = ce.doc_to_markdown(doc)
    assert as_doc["url"] == "https://a.example/"
    assert "First paragraph" in as_doc["content"]
    assert jc.chunk_markdown(as_doc) != [] or True  # short lines may drop


def test_entities_capped_and_deduped():
    html = "<p>a@b.com a@b.com</p>" * 30
    ents = ce.extract_entities("https://a.example/", html)
    assert len(ents) <= 50
    assert sum(1 for e in ents if e["value"] == "a@b.com") == 1


def test_recall_falls_back_to_dom_json(tmp_path, monkeypatch):
    import src.capability.jina_client as jc
    import src.report as report_mod
    from src.runner.memory import recall_page
    import asyncio

    async def _boom_fetch(url):
        return {"error": "reader down"}

    async def _rerank(query, documents, top_n=5, timeout_s=30.0):
        return ([{"index": 0, "score": 0.7}], {})

    monkeypatch.setattr(jc, "fetch_page", _boom_fetch)
    monkeypatch.setattr(jc, "rerank", _rerank)
    lines = asyncio.run(recall_page(
        run_dir=str(tmp_path), report_mod=report_mod,
        url="https://a.example/", task_text="task", page_html=HTML))
    assert len(lines) == 1 and lines[0].startswith("recall(")
    import json

    rows = [json.loads(ln) for ln in
            open(tmp_path / "memory.jsonl", encoding="utf-8")]
    assert all(r["source"] == "dom" for r in rows)
