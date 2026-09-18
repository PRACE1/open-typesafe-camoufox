"""Memory tests — recall formatting, flow, and fail-soft (offline)."""

import asyncio
import json


def _run(coro):
    return asyncio.run(coro)


def test_format_recalled_truncates_and_scores():
    from src.runner.memory import format_recalled

    line = format_recalled("https://a.example/", "Alpha", 0.91234,
                           "word " * 100)
    assert line.startswith("recall(https://a.example/ Alpha 0.91):")
    assert len(line) < 400 and line.endswith("...")


def _fake_jina(monkeypatch, **overrides):
    import src.capability.jina_client as jc

    async def _fetch(url):
        if overrides.get("fetch_error"):
            return {"error": "boom"}
        return {"url": url, "title": "T",
                "content": "# Alpha\n\n" + ("x" * 200) + "\n\n# Beta\n\n"
                           + ("y" * 200),
                "timestamp": ""}

    async def _rerank(query, documents, top_n=5, timeout_s=30.0):
        assert query.startswith("task")
        return ([{"index": 1, "score": 0.8}], {"total_tokens": 5})

    monkeypatch.setattr(jc, "fetch_page", _fetch)
    if overrides.get("rerank_error"):
        async def _bad(*a, **k):
            raise RuntimeError("rerank down")

        monkeypatch.setattr(jc, "rerank", _bad)
    else:
        monkeypatch.setattr(jc, "rerank", _rerank)


def test_recall_page_stores_and_returns_winners(tmp_path, monkeypatch):
    _fake_jina(monkeypatch)
    import src.report as report_mod
    from src.runner.memory import recall_page

    lines = _run(recall_page(run_dir=str(tmp_path), report_mod=report_mod,
                             url="https://a.example/", task_text="task",
                             context_note="ctx"))
    assert len(lines) == 1 and "Beta 0.80" in lines[0]
    rows = [json.loads(ln) for ln in
            open(tmp_path / "memory.jsonl", encoding="utf-8")]
    assert len(rows) == 2
    assert all(r["run_id"] == tmp_path.name for r in rows)
    assert {r["section"] for r in rows} == {"Alpha", "Beta"}
    assert all(r["source"] == "reader" for r in rows)


def test_recall_fail_soft_on_fetch_error(tmp_path, monkeypatch):
    _fake_jina(monkeypatch, fetch_error=True)
    import src.report as report_mod
    from src.runner.memory import recall_page

    assert _run(recall_page(run_dir=str(tmp_path), report_mod=report_mod,
                            url="https://a.example/",
                            task_text="task")) == []
    assert not (tmp_path / "memory.jsonl").exists()


def test_recall_fail_soft_on_rerank_error(tmp_path, monkeypatch):
    _fake_jina(monkeypatch, rerank_error=True)
    import src.report as report_mod
    from src.runner.memory import recall_page

    # Chunks still stored; recall just yields nothing (excerpts kept).
    assert _run(recall_page(run_dir=str(tmp_path), report_mod=report_mod,
                            url="https://a.example/",
                            task_text="task")) == []
    assert (tmp_path / "memory.jsonl").exists()


def test_write_memory_jsonl_envelope(tmp_path):
    from src.report import write_memory_jsonl

    p = write_memory_jsonl(str(tmp_path), [{"url": "u", "text": "t"}])
    row = json.loads(open(p, encoding="utf-8").read().strip())
    assert row["run_id"] == tmp_path.name and row["seq"] >= 1
