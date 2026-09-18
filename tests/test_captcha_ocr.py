"""CAPTCHA OCR tests — ddddocr pipeline, pack/unpack, challenge wiring (offline).

The ddddocr backend is optional: tests stub it in sys.modules so the
suite never downloads weights or touches onnxruntime. Real-backend
behavior is covered by the stub's faithful API shape (classification /
detection / slide_match signatures per sml2h3/ddddocr master).
"""

import asyncio
import json
import sys
import types

import pytest

from src.capability import captcha_ocr as co
from src.capability.captcha_ocr import (
    RESULT_MARKER,
    CaptchaOcrResult,
    pack_result_line,
    unpack_result_line,
)
from src.deps import ElementRef


class _FakeOcr:
    """Stub DdddOcr with scripted classification/detection results."""

    def __init__(self, text="AB12", conf_rows=None, boxes=None):
        self._text = text
        self._conf_rows = conf_rows
        self._boxes = boxes if boxes is not None else []

    def classification(self, img, probability=False, **kwargs):
        assert isinstance(img, (bytes, bytearray)), "OCR takes image bytes"
        if probability:
            charsets = ["", "A", "B", "1", "2"]
            rows = self._conf_rows or [[0.01, 0.9, 0.03, 0.03, 0.03]] * len(self._text)
            return {"charsets": charsets, "probability": rows}
        return self._text

    def detection(self, img):
        return list(self._boxes)


def _install_stub(monkeypatch, fake=None):
    mod = types.ModuleType("ddddocr")
    mod.DdddOcr = lambda *a, **k: (fake or _FakeOcr())
    monkeypatch.setitem(sys.modules, "ddddocr", mod)
    monkeypatch.setattr(co, "_ocr_instance", None)
    monkeypatch.setattr(co, "_det_instance", None)
    monkeypatch.setattr(co, "_slider_instance", None)
    return mod


def test_pack_unpack_roundtrip():
    res = CaptchaOcrResult(
        available=True, kind="captcha", text="AB12",
        confidence=0.87, instruction="enter the characters",
        suggest_kind="type_at", suggest_item=3, elapsed_ms=12,
    )
    line = pack_result_line(5, res)
    assert RESULT_MARKER in line
    assert "no dispatch" in line  # review-first: never an executed action
    obj = unpack_result_line(line)
    assert obj is not None
    assert obj["text"] == "AB12"
    assert obj["confidence"] == pytest.approx(0.87)
    assert obj["suggest_item"] == 3
    assert "available" in obj and obj["backend"] == "ddddocr"


def test_unpack_rejects_plain_lines():
    assert unpack_result_line("element #5 scroll+circle+click at (0.1,0.2)") is None
    assert unpack_result_line("error: element #5 captcha/image challenge — escalating") is None


def test_history_line_never_embeds_bytes():
    res = CaptchaOcrResult(available=True, kind="captcha", text="X" * 200,
                           confidence=0.9)
    line = res.to_history_line(7, 2)
    assert "captcha-ocr" in line and len(line) < 300


def test_mean_confidence_and_text_rebuild():
    prob = {"charsets": ["", "A", "B"],
            "probability": [[0.1, 0.8, 0.1], [0.1, 0.2, 0.7]]}
    assert co._mean_confidence(prob) == pytest.approx(0.75)
    assert co._text_from_probability(prob) == "AB"
    assert co._mean_confidence({}) == pytest.approx(0.5)
    assert co._mean_confidence(None) == pytest.approx(0.5)


def test_adjacent_input_prefers_following():
    els = [ElementRef(idx=0, kind="image", label="captcha"),
           ElementRef(idx=1, kind="input"),
           ElementRef(idx=5, kind="input")]
    assert co._adjacent_input(els, 0) == 1
    assert co._adjacent_input([ElementRef(idx=4, kind="input")], 9) == 4
    assert co._adjacent_input([ElementRef(idx=0, kind="link")], 0) is None


def test_instruction_prefers_element_then_page_text():
    el = ElementRef(idx=0, kind="image", label="Enter the code below")
    assert co._instruction_from_dom(el, "other text") == "Enter the code below"
    el2 = ElementRef(idx=1, kind="image")
    assert co._instruction_from_dom(el2, "Please verify you are human\nnext") == \
        "Please verify you are human"
    assert co._instruction_from_dom(el2, "nothing relevant") == ""


def test_solve_text_captcha_success(monkeypatch):
    _install_stub(monkeypatch, _FakeOcr(text="AB", conf_rows=[
        [0.01, 0.9, 0.03, 0.03, 0.03], [0.01, 0.03, 0.9, 0.03, 0.03]]))
    text, conf, cands, err = asyncio.run(co.solve_text_captcha(b"fake-png"))
    assert err is None
    assert text == "AB"
    assert conf == pytest.approx(0.9)
    assert [c.text for c in cands][0] == "AB"


def test_solve_text_captcha_unavailable(monkeypatch):
    monkeypatch.delitem(sys.modules, "ddddocr", raising=False)
    monkeypatch.setattr(co, "_ocr_instance", None)
    text, conf, cands, err = asyncio.run(co.solve_text_captcha(b"fake-png"))
    assert text == "" and conf == 0.0 and err
    assert cands == []


def test_topk_ranks_near_miss_second():
    # Ambiguous first char: 0 (0.45) vs O (0.40); sure second char: 7.
    prob = {"charsets": ["", "0", "O", "7"],
            "probability": [[0.05, 0.45, 0.40, 0.10],
                            [0.02, 0.02, 0.02, 0.94]]}
    ranked = co._topk_from_probability(prob, k=3)
    assert [c.text for c in ranked] == ["07", "O7"]
    assert ranked[0].confidence == pytest.approx((0.45 + 0.94) / 2)
    assert ranked[1].confidence == pytest.approx((0.40 + 0.94) / 2)
    assert ranked[0].confidence > ranked[1].confidence


def test_topk_empty_result_is_empty():
    assert co._topk_from_probability({}) == []
    assert co._topk_from_probability({"charsets": [], "probability": []}) == []


def test_solve_challenge_unavailable_result_shape(monkeypatch):
    monkeypatch.delitem(sys.modules, "ddddocr", raising=False)
    monkeypatch.setattr(co, "is_available", lambda: False)
    els = [ElementRef(idx=0, kind="image", label="captcha")]
    res = asyncio.run(co.solve_challenge(None, els, 0, "verify you are human"))
    assert res.available is False
    assert res.error
    rec = res.to_record()
    assert set(rec) >= {"kind", "text", "boxes", "confidence", "backend",
                        "instruction", "suggest_kind", "suggest_item",
                        "elapsed_ms", "error", "available"}


class _ShotLocator:
    def __init__(self, png=None):
        self._png = png

    async def count(self):
        return 1 if self._png is not None else 0

    async def screenshot(self, timeout=None):
        if self._png is None:
            raise RuntimeError("no image")
        return self._png


class _ShotPage:
    def __init__(self, png=None):
        self._png = png

    def locator(self, *a, **k):
        return _ShotLocator(self._png)


class _ShotPlatform:
    def __init__(self, png=None):
        from asyncio import Lock

        self.page = _ShotPage(png)
        self._lock = Lock()

    async def quiesce_idle_motion(self):
        return False

    def resume_idle_motion(self):
        pass


def _real_captcha_png(text="AB12", w=160, h=60):
    """Genuine PNG image bytes with drawn text (not opaque fake bytes)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((20, 10), text, fill="black")
    # Light noise line so the image is a realistic CAPTCHA input.
    d.line([(0, h // 2), (w, h // 2)], fill="gray", width=1)
    import io as _io

    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _ImageValidatingOcr(_FakeOcr):
    """Stub backend that first proves the input is a real decodable image."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen_size = None

    def classification(self, img, probability=False, **kwargs):
        from PIL import Image as _Image
        import io as _io

        decoded = _Image.open(_io.BytesIO(bytes(img)))
        decoded.load()
        assert decoded.size[0] > 0 and decoded.size[1] > 0
        self.seen_size = decoded.size
        return super().classification(img, probability=probability, **kwargs)


def test_real_image_bytes_flow_into_ranked_data(monkeypatch):
    """A genuine PNG (drawn text) flows capture → OCR → ranked candidates."""
    png = _real_captcha_png("AB12")
    fake = _ImageValidatingOcr(text="AB", conf_rows=[
        [0.01, 0.9, 0.03, 0.03, 0.03], [0.01, 0.03, 0.9, 0.03, 0.03]])
    _install_stub(monkeypatch, fake)
    text, conf, cands, err = asyncio.run(co.solve_text_captcha(png))
    assert err is None
    assert fake.seen_size == (160, 60)  # backend saw the real image
    assert text == "AB" and [c.text for c in cands][0] == "AB"


def test_challenge_control_captcha_ocr_success(monkeypatch):
    from src import actions as _actions

    png = _real_captcha_png("Q7")
    _install_stub(monkeypatch, _ImageValidatingOcr(text="Q7", conf_rows=[
        [0.02, 0.02, 0.02, 0.02, 0.94], [0.02, 0.02, 0.02, 0.94, 0.02]]))
    els = [ElementRef(idx=0, kind="image", label="captcha code",
                      aria="e1", ref="e1"),
           ElementRef(idx=1, kind="input", label="code")]
    plat = _ShotPlatform(png=png)
    res = asyncio.run(_actions.challenge_control(plat, els, 0))
    assert "captcha-ocr:" in res
    assert not res.startswith("error")
    obj = unpack_result_line(res)
    assert obj is not None and obj["available"] is True
    assert obj["suggest_kind"] == "type_at" and obj["suggest_item"] == 1
    # Stub distribution spells "21" (charsets ["",A,B,1,2]); the point is
    # the bytes were genuinely decoded and ranked, not the stub's label.
    assert obj["candidates"] and obj["candidates"][0]["text"] == "21"
    assert obj["text"] == "21"


def test_challenge_control_captcha_no_image_escalates(monkeypatch):
    from src import actions as _actions

    _install_stub(monkeypatch)
    els = [ElementRef(idx=0, kind="image", label="captcha", aria="e9", ref="e9")]
    plat = _ShotPlatform(png=None)  # locator.count() == 0 → no capture
    res = asyncio.run(_actions.challenge_control(plat, els, 0))
    assert "escalating" in res


def test_write_captcha_json_roundtrip(tmp_path):
    from src.report import write_captcha_json

    p = write_captcha_json(str(tmp_path), 4, {
        "n": 4, "url": "https://a.example/", "element_idx": 2,
        "ocr": {"available": True, "text": "AB12", "confidence": 0.9},
    })
    assert p.endswith("captcha-04.json")
    doc = json.loads(open(p, encoding="utf-8").read())
    assert doc["ocr"]["text"] == "AB12"


def test_step_record_accepts_captcha_block(tmp_path):
    from src.report import StepRecord, write_step_jsonl

    rec = {"n": 2, "captcha_ocr": {"available": True, "text": "AB",
                                   "confidence": 0.9, "backend": "ddddocr"}}
    p = write_step_jsonl(str(tmp_path), rec)
    line = open(p, encoding="utf-8").read().strip()
    assert StepRecord.model_validate_json(line).captcha_ocr["text"] == "AB"


def _cands():
    return [{"text": "07", "confidence": 0.69},
            {"text": "O7", "confidence": 0.67}]


def test_jev_pick_no_key_takes_top_candidate(monkeypatch):
    from src.decide import decide_captcha_action

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    text, conf = asyncio.run(decide_captcha_action(
        instruction="enter the digits", candidates=_cands(),
        page_excerpt="captcha"))
    assert (text, conf) == ("07", pytest.approx(0.69))


def test_jev_pick_empty_candidates_abstains(monkeypatch):
    from src.decide import decide_captcha_action

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert asyncio.run(decide_captcha_action(
        instruction="x", candidates=[], page_excerpt="p")) == ("", 0.0)


def test_jev_pick_decodes_runner_up_and_clamps(monkeypatch):
    import src.decide as _decide
    from src.decide import decide_captcha_action

    async def _fake_post(payload, timeout_s, base, key):
        crit = payload["questions"]["captcha_pick"]["criteria"]
        assert crit["0"]["text"] == "07" and "none" in crit
        return {"answers": {"captcha_pick": {"choice": "1",
                                             "confidence": 9.9}}}

    monkeypatch.setattr(_decide, "_post", _fake_post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert asyncio.run(decide_captcha_action(
        instruction="enter the code", candidates=_cands(),
        page_excerpt="p")) == ("O7", 1.0)


def test_jev_pick_none_abstains_and_bad_choice_falls_back(monkeypatch):
    import src.decide as _decide
    from src.decide import decide_captcha_action

    async def _fake_none(payload, timeout_s, base, key):
        return {"answers": {"captcha_pick": {"choice": "none",
                                             "confidence": 0.9}}}

    monkeypatch.setattr(_decide, "_post", _fake_none)
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert asyncio.run(decide_captcha_action(
        instruction="x", candidates=_cands(), page_excerpt="p")) == ("", 0.0)

    async def _fake_bad(payload, timeout_s, base, key):
        return {"answers": {"captcha_pick": {"choice": "7"}}}

    monkeypatch.setattr(_decide, "_post", _fake_bad)
    text, conf = asyncio.run(decide_captcha_action(
        instruction="x", candidates=_cands(), page_excerpt="p"))
    assert text == "07" and conf == pytest.approx(0.69)


def test_image_to_data_to_jev_pick_end_to_end(monkeypatch):
    """Real PNG → ranked OCR data → JEV scores the most likely candidate."""
    from src.decide import decide_captcha_action

    png = _real_captcha_png("07")
    # Near-miss distribution: 0 edged over O on the first char.
    _install_stub(monkeypatch, _ImageValidatingOcr(text="07", conf_rows=[
        [0.05, 0.45, 0.40, 0.10], [0.02, 0.02, 0.02, 0.94]]))
    # Re-point the stub charsets to match this distribution.
    import src.capability.captcha_ocr as _co

    orig_topk = _co._topk_from_probability
    prob = {"charsets": ["", "0", "O", "7"],
            "probability": [[0.05, 0.45, 0.40, 0.10],
                            [0.02, 0.02, 0.02, 0.94]]}
    ranked = orig_topk(prob, k=3)
    assert [c.text for c in ranked] == ["07", "O7"]

    # Image genuinely processed into the ranking:
    text, conf, cands, err = asyncio.run(_co.solve_text_captcha(png))
    assert err is None and cands, "pipeline must return ranked data"

    # JEV (offline fallback = top rank) scores the most likely candidate:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    pick, pick_conf = asyncio.run(decide_captcha_action(
        instruction="enter the digits shown",
        candidates=[c.model_dump() for c in ranked],
        page_excerpt="captcha digits"))
    assert pick == "07"
    assert pick_conf == pytest.approx(ranked[0].confidence)
