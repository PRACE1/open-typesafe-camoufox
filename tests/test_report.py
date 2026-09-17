"""Report tests — annotation, payload/answers roundtrip, replay (offline)."""

import io
import json
import os

from PIL import Image

from src.deps import ElementRef
from src.report import (
    annotate_png, append_memory, load_lessons, make_run_dir, replay_payload,
    write_answers_json, write_payload_txt, write_raw_png,
)


def _png_bytes(w=200, h=100, color="white"):
    img = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_annotate_png_same_size_and_marks():
    raw = _png_bytes()
    els = [ElementRef(idx=0, kind="link", cx=0.25, cy=0.5),
           ElementRef(idx=1, kind="input", cx=0.75, cy=0.5)]
    out = annotate_png(raw, els, chosen_idx=1,
                       focused_frame={"x": 140, "y": 40, "w": 40, "h": 20})
    img = Image.open(io.BytesIO(out))
    assert img.size == (200, 100)
    assert out != raw  # rings were drawn


def test_annotate_png_empty_elements():
    raw = _png_bytes()
    assert Image.open(io.BytesIO(annotate_png(raw, []))).size == (200, 100)


def test_payload_and_answers_roundtrip(tmp_path):
    run_dir = str(tmp_path)
    state = {"task": "t", "elements": [{"idx": 0}]}
    questions = {"kind": {"type": "choice"}}
    p = write_payload_txt(run_dir, 3, state, questions, "click_item conf=0.90 item=0")
    assert os.path.exists(p)
    body = open(p, encoding="utf-8").read()
    assert "click_item conf=0.90" in body and '"task": "t"' in body
    raw_png = write_raw_png(run_dir, 3, _png_bytes())
    assert os.path.exists(raw_png)
    a = write_answers_json(run_dir, 3, {"answers": {"kind": {"choice": "click_item"}}})
    assert os.path.exists(a)
    loaded = replay_payload(run_dir, 3)
    assert loaded["answers"]["answers"]["kind"]["choice"] == "click_item"
    assert replay_payload(run_dir, 99) == {}


def test_make_run_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = make_run_dir()
    assert os.path.isdir(d) and os.path.isdir(os.path.join(d))
    assert json.dumps({"ok": True})


def test_append_memory_notebook(tmp_path):
    from src.report import append_memory as _am
    p = _am(str(tmp_path), "- https://a.example/ :: excerpt")
    body = open(p, encoding="utf-8").read()
    assert "- https://a.example/ :: excerpt\n" in body
    _am(str(tmp_path), "OUTCOME done=True")
    assert open(p, encoding="utf-8").read().count("\n") == 2


def test_load_lessons_bounded_and_missing(tmp_path):
    mem = tmp_path / ".agent-memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert load_lessons(str(tmp_path), limit=100) == "line1\nline2\nline3"
    short = load_lessons(str(tmp_path), limit=8)
    assert len(short) <= 8 and short.startswith("line1")
    assert load_lessons(str(tmp_path / "nope")) == ""
