"""captcha_ocr.py — ddddocr-backed CAPTCHA image pipeline (optional backend).

Upstream: https://github.com/sml2h3/ddddocr (MIT, ~14.8k stars).
API verified against master (compat/v1.py): ``DdddOcr(ocr, det, ...)`` with
``classification(img, png_fix, probability, ...)``,
``detection(img)``, ``slide_match(target, background, simple_target)``,
``slide_comparison(a, b)``, ``set_ranges(...)``.

Role in the solver: this module NEVER dispatches browser input. It
captures the challenge image, runs the local OCR/detection pipeline,
and returns a structured, confidence-scored result. The runner records
it as JSONL and JEV reviews it before any interaction happens — the
next step's normal propose/decide path (usually ``type_at`` into the
adjacent input) is what acts, never this module.

Backend lifecycle: ddddocr model load is slow (~seconds, downloads
weights on first use) and must happen once, not per step. Instances
are process singletons per mode (ocr / det / slider), created lazily
on first use. ``ddddocr`` is an optional import: when it (or its
``onnxruntime`` dependency) is missing, every entry point returns a
structured ``available=False`` result instead of raising, so the
challenge flow degrades to the pre-OCR behavior (escalate, keep trying
alternate routes).
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from pydantic import BaseModel, Field

from .aria_refs import resolve_ref
from .logging_utils import log

BACKEND_NAME = "ddddocr"
MIN_CONFIDENCE_TO_SUGGEST = 0.35


class CaptchaBox(BaseModel):
    """One detected region: label + pixel box + confidence."""

    model_config = {"extra": "ignore"}

    label: str = ""
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    confidence: float = 0.0


class CaptchaCandidate(BaseModel):
    """One ranked reading of the CAPTCHA image: text + confidence.

    Ranked best-first. JEV picks among these (see
    ``decide.decide_captcha_action``) instead of trusting a single
    argmax string — a low-confidence top read loses to a surer
    runner-up when the instruction context favors it.
    """

    model_config = {"extra": "ignore"}

    text: str = ""
    confidence: float = 0.0


class CaptchaOcrResult(BaseModel):
    """Structured outcome of one CAPTCHA analysis pass.

    Serialized into the step JSONL (``captcha_ocr`` block) and the
    per-step ``captcha-NN.json`` audit file. ``suggest_*`` fields are a
    *proposal* for JEV to review — never an executed action.
    """

    model_config = {"extra": "ignore"}

    available: bool = False
    kind: str = "none"
    text: str = ""
    candidates: list[CaptchaCandidate] = Field(default_factory=list)
    boxes: list[CaptchaBox] = Field(default_factory=list)
    confidence: float = 0.0
    backend: str = BACKEND_NAME
    instruction: str = ""
    suggest_kind: str = ""
    suggest_item: int | None = None
    elapsed_ms: int = 0
    error: str | None = None

    def to_record(self) -> dict[str, Any]:
        """Compact dict for JSONL audit (no raw image bytes, ever)."""
        return {
            "available": self.available,
            "kind": self.kind,
            "text": self.text,
            "candidates": [c.model_dump() for c in self.candidates],
            "boxes": [b.model_dump() for b in self.boxes],
            "confidence": round(self.confidence, 3),
            "backend": self.backend,
            "instruction": self.instruction[:200],
            "suggest_kind": self.suggest_kind,
            "suggest_item": self.suggest_item,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }

    def to_history_line(self, step: int, idx: int) -> str:
        """One-line human feed entry (no image bytes, text truncated)."""
        if not self.available:
            return (f"step {step}: captcha OCR unavailable "
                    f"({self.error or 'backend missing'}) — "
                    f"challenge #{idx} left for JEV/manual routes")
        if self.error:
            return (f"step {step}: captcha OCR failed on #{idx} "
                    f"({self.error}) — will retry or route around")
        detail = self.text or f"{len(self.boxes)} box(es)"
        sugg = ""
        if self.suggest_kind and self.suggest_item is not None:
            sugg = f" — suggest {self.suggest_kind} #{self.suggest_item} for JEV review"
        return (f"step {step}: captcha-ocr #{idx} kind={self.kind} "
                f"text={detail!r:.60} conf={self.confidence:.2f}{sugg}")


RESULT_MARKER = "captcha-ocr:"


def pack_result_line(idx: int, result: "CaptchaOcrResult") -> str:
    """Single-line action result carrying the structured OCR payload.

    The JSON after the marker is the machine channel: the runner parses
    it back out for the ``captcha_ocr`` entry block, the ``captcha-NN.json``
    audit file, and the canonical steps.jsonl record. Human readers get
    the trailing summary; no image bytes are ever embedded.
    """
    import json as _json

    payload = _json.dumps(result.to_record(), ensure_ascii=False)
    summary = result.text[:40] if result.text else f"{len(result.boxes)} box(es)"
    return (f"element #{idx} {RESULT_MARKER}{payload} "
            f"conf={result.confidence:.2f} text={summary!r:.44} "
            f"awaiting JEV review (no dispatch)")


def unpack_result_line(line: str) -> dict | None:
    """Extract the structured OCR payload from a packed result line."""
    import json as _json

    try:
        start = line.index(RESULT_MARKER) + len(RESULT_MARKER)
    except ValueError:
        return None
    tail = line[start:].strip()
    # Payload is the first balanced {...} run in the tail.
    depth = 0
    for i, ch in enumerate(tail):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = _json.loads(tail[:i + 1])
                except ValueError:
                    return None
                return obj if isinstance(obj, dict) else None
    return None


_lock = threading.Lock()
_ocr_instance: Any = None
_det_instance: Any = None
_slider_instance: Any = None
_unavailable_reason: str | None = None


def is_available() -> bool:
    """True when the ddddocr backend imports (no model load)."""
    try:
        import ddddocr  # noqa: F401
        return True
    except Exception as exc:  # noqa: BLE001
        return False


def _import_backend() -> Any:
    """Import ddddocr or raise ImportError with the recorded reason."""
    global _unavailable_reason
    try:
        import ddddocr

        _unavailable_reason = None
        return ddddocr
    except Exception as exc:  # noqa: BLE001
        _unavailable_reason = f"{exc.__class__.__name__}: {exc}"
        raise ImportError(_unavailable_reason)


def _get_instance(mode: str) -> Any:
    """Process-singleton DdddOcr per mode (ocr / det / slider)."""
    global _ocr_instance, _det_instance, _slider_instance
    ddddocr = _import_backend()
    with _lock:
        if mode == "ocr":
            if _ocr_instance is None:
                _ocr_instance = ddddocr.DdddOcr(show_ad=False)
            return _ocr_instance
        if mode == "det":
            if _det_instance is None:
                _det_instance = ddddocr.DdddOcr(det=True, show_ad=False)
            return _det_instance
        if _slider_instance is None:
            _slider_instance = ddddocr.DdddOcr(det=False, ocr=False, show_ad=False)
        return _slider_instance


def _mean_confidence(prob_result: Any) -> float:
    """Mean per-character max probability from a probability=True result."""
    try:
        probs = prob_result.get("probability") if isinstance(prob_result, dict) else None
        if not probs:
            return 0.5  # backend answered without a distribution: neutral
        scores = [max(row) for row in probs if row]
        if not scores:
            return 0.5
        return min(max(sum(scores) / len(scores), 0.0), 1.0)
    except Exception:  # noqa: BLE001
        return 0.5


def _text_from_probability(prob_result: Any) -> str:
    """Rebuild the string from a probability=True result."""
    try:
        charsets = prob_result.get("charsets", [])
        out = ""
        for row in prob_result.get("probability", []):
            if not row:
                continue
            out += str(charsets[int(max(range(len(row)), key=row.__getitem__))])
        return out
    except Exception:  # noqa: BLE001
        return ""


def _topk_from_probability(prob_result: Any, k: int = 3) -> list[CaptchaCandidate]:
    """Rank the top-k readings of a probability=True OCR result.

    Rank 1 is the per-position argmax. Ranks 2..k flip the least-confident
    positions to their second-best character — the classic near-miss set
    (e.g. ``0`` vs ``O``, ``1`` vs ``I``). Scores are the mean per-position
    probability with the substitution applied, so the ranking is a real
    ordering by model confidence, not a guess. Empty result → [].
    """
    try:
        charsets = list(prob_result.get("charsets", []))
        rows = [list(r) for r in (prob_result.get("probability", []) or []) if r]
        if not charsets or not rows:
            return []
        order = []
        for row in rows:
            ranked = sorted(range(len(row)), key=lambda i: row[i], reverse=True)
            order.append(ranked)
        best = [charsets[o[0]] if o[0] < len(charsets) else "" for o in order]
        confs = [row[o[0]] for row, o in zip(rows, order)]
        cands = [CaptchaCandidate(
            text="".join(best),
            confidence=min(max(sum(confs) / len(confs), 0.0), 1.0),
        )]
        # Least-confident positions first: each yields one variant.
        positions = sorted(range(len(rows)), key=lambda i: confs[i])
        for pos in positions[: max(0, k - 1)]:
            if len(order[pos]) < 2:
                continue
            alt = list(best)
            alt[pos] = (charsets[order[pos][1]]
                        if order[pos][1] < len(charsets) else "")
            alt_confs = list(confs)
            alt_confs[pos] = rows[pos][order[pos][1]]
            text = "".join(alt)
            if len(text) != len(cands[0].text):
                continue  # substitution must not add/drop characters
            if text and text != cands[0].text:
                cands.append(CaptchaCandidate(
                    text=text,
                    confidence=min(max(sum(alt_confs) / len(alt_confs), 0.0), 1.0),
                ))
            if len(cands) >= k:
                break
        # Best-first, deduped.
        seen: set[str] = set()
        ranked = []
        for c in sorted(cands, key=lambda c: c.confidence, reverse=True):
            if c.text and c.text not in seen:
                seen.add(c.text)
                ranked.append(c)
        return ranked[:k]
    except Exception:  # noqa: BLE001
        return []


async def _capture_element_png(platform, aria: str,
                               timeout_s: float = 10.0) -> bytes | None:
    """Screenshot the challenge element via its native aria ref.

    Locator screenshots are scoped to the element — no full-page bytes
    are kept. None when the ref is missing or the capture fails.
    """
    if not aria:
        return None
    try:
        locator = platform.page.locator(resolve_ref(aria, {aria}))
        if await locator.count() == 0:
            return None
        return await asyncio.wait_for(
            locator.screenshot(timeout=int(timeout_s * 1000)),
            timeout=timeout_s + 2.0,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"[captcha-ocr] element capture failed: {exc}")
        return None


def _instruction_from_dom(el, page_text: str) -> str:
    """CAPTCHA instruction from the element + nearby page text (≤200ch)."""
    bits = " ".join(
        s for s in (
            getattr(el, "label", "") or "",
            getattr(el, "placeholder", "") or "",
            getattr(el, "text", "") or "",
        ) if s
    ).strip()
    if bits:
        return bits[:200]
    # Fall back to the densest captcha-mentioning line in page text.
    for line in (page_text or "").splitlines():
        low = line.lower()
        if any(k in low for k in ("captcha", "verify", "robot", "puzzle", "characters")):
            return line.strip()[:200]
    return ""


def _adjacent_input(elements: list, idx: int) -> int | None:
    """Idx of the nearest text-entry element after the challenge node.

    CAPTCHA widgets pair an image with an input; the OCR text belongs
    in that input. Nearest following input wins, else nearest input
    overall, else None (JEV then decides without a suggestion).
    """
    entry_kinds = {"input", "textarea", "select", "combobox", "searchbox"}
    inputs = [e for e in elements if e.kind in entry_kinds]
    if not inputs:
        return None
    following = [e for e in inputs if e.idx > idx]
    if following:
        return min(following, key=lambda e: e.idx).idx
    return min(inputs, key=lambda e: abs(e.idx - idx)).idx


async def solve_text_captcha(
    png: bytes,
) -> tuple[str, float, list[CaptchaCandidate], str | None]:
    """OCR one CAPTCHA image → (text, confidence, ranked candidates, error).

    Blocking model inference runs off-thread. ``candidates`` is the
    best-first ranking from the probability distribution (see
    ``_topk_from_probability``); JEV picks among them. A plain-string
    backend reply yields a single neutral-confidence candidate.
    """
    def _run() -> tuple[str, float, list[CaptchaCandidate], str | None]:
        try:
            ocr = _get_instance("ocr")
        except ImportError as exc:
            return "", 0.0, [], str(exc)
        try:
            res = ocr.classification(png, probability=True)
        except Exception as exc:  # noqa: BLE001
            return "", 0.0, [], f"classification failed: {exc}"
        if isinstance(res, dict):
            cands = _topk_from_probability(res)
            if not cands:
                return "", 0.0, [], "empty OCR result"
            top = cands[0]
            return top.text, top.confidence, cands, None
        if isinstance(res, str) and res.strip():
            text = res.strip()
            cands = [CaptchaCandidate(text=text, confidence=0.5)]
            return text, 0.5, cands, None  # no distribution: neutral
        return "", 0.0, [], "empty OCR result"

    return await asyncio.to_thread(_run)


async def solve_detection(png: bytes) -> tuple[list[list[int]], str | None]:
    """Detection boxes for grid-style CAPTCHAs → (bboxes, error)."""
    def _run() -> tuple[list[list[int]], str | None]:
        try:
            det = _get_instance("det")
        except ImportError as exc:
            return [], str(exc)
        try:
            boxes = det.detection(png)
            return [[int(v) for v in b] for b in (boxes or [])], None
        except Exception as exc:  # noqa: BLE001
            return [], f"detection failed: {exc}"

    return await asyncio.to_thread(_run)


async def solve_challenge(platform, elements: list, idx: int,
                          page_text: str = "") -> CaptchaOcrResult:
    """Full pipeline for one challenge element: capture → analyze → suggest.

    1. Locate the challenge node; extract the DOM instruction.
    2. Wait for the element to be attached, then screenshot it.
    3. Run text OCR (probability=True → text + confidence); on grids
       also run detection for bounding boxes.
    4. Suggest the adjacent input + ``type_at`` when confidence clears
       ``MIN_CONFIDENCE_TO_SUGGEST`` — a proposal for JEV, not a dispatch.

    Never raises; every failure is a structured result with ``error`` set.
    Never touches the mouse, keyboard, or any input element.
    """
    t0 = time.monotonic()
    el = next((e for e in elements if e.idx == idx), None)
    if el is None:
        return CaptchaOcrResult(
            available=is_available(), kind="none",
            error=f"challenge #{idx} gone before capture",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    if not is_available():
        return CaptchaOcrResult(
            available=False, kind="captcha",
            instruction=_instruction_from_dom(el, page_text),
            error=_unavailable_reason or "ddddocr not installed",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    instruction = _instruction_from_dom(el, page_text)
    png = await _capture_element_png(platform, getattr(el, "aria", "") or "")
    if not png:
        return CaptchaOcrResult(
            available=True, kind="captcha", instruction=instruction,
            error="element capture produced no image (ref gone or hidden)",
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )
    text, conf, candidates, err = await solve_text_captcha(png)
    boxes: list[CaptchaBox] = []
    if not text or conf < MIN_CONFIDENCE_TO_SUGGEST:
        # Low-confidence text reads on grid CAPTCHAs: add detection
        # boxes so JEV still gets structured regions to reason over.
        det_boxes, _ = await solve_detection(png)
        for i, b in enumerate(det_boxes[:16]):
            if len(b) >= 4:
                boxes.append(CaptchaBox(
                    label=f"cell-{i}", x1=b[0], y1=b[1],
                    x2=b[2], y2=b[3], confidence=0.0,
                ))
    suggest_item = _adjacent_input(elements, idx) if text and conf >= MIN_CONFIDENCE_TO_SUGGEST else None
    return CaptchaOcrResult(
        available=True, kind="captcha", text=text,
        candidates=candidates,
        boxes=boxes, confidence=conf,
        instruction=instruction,
        suggest_kind="type_at" if suggest_item is not None else "",
        suggest_item=suggest_item,
        elapsed_ms=int((time.monotonic() - t0) * 1000),
        error=err,
    )
