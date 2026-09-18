"""tiles.py — image-grid tile geometry + labels (pure; no browser).

Port of the tile helpers of
https://github.com/BetterWright/betterwright/blob/main/src/captcha-solver.ts
(``dedupeBoxes`` / ``sortTilesReadingOrder`` / ``collapseNestedBoxes`` /
``clusterSimilarBoxes`` / ``gridFromTiles`` / ``inferGridTiles`` /
``parseTileIndexes`` / chrome + skip/verify label filters / tile &
instruction selector tables). Reimplemented in Python against plain
``{"x","y","width","height"}`` dicts. Clean-room behavior parity.

Pipeline these compose (all pure, fully unit-testable):
  raw boxes → dedupe → cluster similar sizes → collapse nested
  selected-state wrappers → reading-order sort → grid dims →
  plausible-grid gate → per-tile work.

The browser half (finding tile elements in the bframe, screenshotting
each) lives in ``captcha_ocr``; it feeds boxes in here and gets ordered
tile indexes out.
"""

from __future__ import annotations

import re
from typing import Any

# -- tile discovery selectors (ported from their *_SELECTORS tables) --------
# Ordered most-specific first. Used against the bframe frame-locator.

IMAGE_TILE_SELECTORS = (
    "td.rc-imageselect-tile",
    ".rc-imageselect-tile",
    ".rc-image-tile-wrapper",
    ".task-image",
    "[class*='task-image']",
    "[role='button'][aria-label]",
    "div[class*='tile']",
)

SELECTED_IMAGE_TILE_SELECTORS = (
    ".rc-imageselect-tileselected",
    ".rc-imageselect-tile.selected",
    ".rc-imageselect-tile[aria-pressed='true']",
)

CHALLENGE_INSTRUCTION_SELECTORS = (
    ".rc-imageselect-desc-wrapper",
    ".rc-imageselect-desc-no-canonical",
    ".rc-imageselect-instructions",
    ".prompt-text",
    ".challenge-text",
    "h2",
    "h1",
)

VERIFY_BUTTON_SELECTORS = (
    "#recaptcha-verify-button",
    "button",
    "[role='button']",
)

# -- chrome / submit label filters -------------------------------------------

_CHROME_LABEL = re.compile(
    r"skip challenge|refresh challenge|select a language|accessibility|"
    r"hcaptcha logo|recaptcha logo|privacy policy|terms of service|"
    r"about hcaptcha|opens new window|^skip$|^en$|^english\b|^menu\b|"
    r"^about\b|^refresh$|^reload$",
    re.IGNORECASE)
_SKIP_SUBMIT_LABEL = re.compile(
    r"^(skip|reload|refresh|omitir|ignorar|pular|saltar|passer|ignorer|"
    r"überspringen|uberspringen|salta|overslaan|пропустить|スキップ|"
    r"跳过|건너뛰기)(?:\s+challenge)?$",
    re.IGNORECASE)
_VERIFY_SUBMIT_LABEL = re.compile(
    r"^(verify|next|submit|continue|i am human|check)$", re.IGNORECASE)


def _clean(label: Any) -> str:
    return re.sub(r"\s+", " ", str(label or "")).strip()


def is_chrome_label(label: Any) -> bool:
    """Footer/language/skip controls that look like tiles but are chrome."""
    text = _clean(label)
    return bool(text) and bool(_CHROME_LABEL.search(text))


def is_skip_submit_label(label: Any) -> bool:
    """reCAPTCHA's verify button reads Skip until a tile is selected —
    clicking it then fetches a NEW puzzle instead of submitting."""
    text = _clean(label)
    return bool(text) and bool(_SKIP_SUBMIT_LABEL.match(text))


def is_verify_submit_label(label: Any) -> bool:
    """Positive name for a control that submits the current challenge."""
    text = _clean(label)
    return bool(text) and bool(_VERIFY_SUBMIT_LABEL.match(text))


def is_verify_ready(label: Any, previous_label: Any = "",
                    had_selection: Any = False) -> bool:
    """Whether the verify button is safe to click as Verify.

    Unrecognized nonempty labels are NOT treated as Verify on their own
    (a finite Skip-word list can't cover every locale): fall back to the
    label-change heuristic — no prior selection + label change means Skip
    became Verify; prior selection + unchanged label means still Verify;
    prior selection + change means tiles were toggled off.
    """
    text = _clean(label)
    if not text or is_skip_submit_label(text):
        return False
    if is_verify_submit_label(text):
        return True
    previous = _clean(previous_label)
    if not previous:
        return False
    selected_before = bool(had_selection) if isinstance(
        had_selection, bool) else False
    if text != previous:
        return not selected_before
    return selected_before


# -- box helpers ---------------------------------------------------------------

TILE_INDEX_MAX = 63


def parse_tile_indexes(value: Any) -> list[int]:
    """Normalize host-vision tile picks ([0,3,5], "0,3,5", or {index})."""
    if value is None:
        return []
    if isinstance(value, dict):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = str(value).split()
        flat: list[Any] = []
        for part in raw:
            flat.extend(part.split(","))
        raw = flat
    indexes: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            n = int(item["index"] if isinstance(item, dict) else item)
        except (ValueError, TypeError, KeyError):
            continue
        if 0 <= n <= TILE_INDEX_MAX and n not in seen:
            seen.add(n)
            indexes.append(n)
    return indexes


def _box(b: dict[str, Any]) -> dict[str, float]:
    def num(key: str) -> float:
        try:
            return float(b.get(key) or 0)
        except (ValueError, TypeError):
            return 0.0

    return {"x": round(num("x")), "y": round(num("y")),
            "width": round(num("width")), "height": round(num("height"))}


def dedupe_boxes(boxes: list[dict[str, Any]] | None,
                 quantum: int = 4) -> list[dict[str, float]]:
    """Round + drop sub-8px scraps + quantize-dedupe overlapping reports."""
    out: list[dict[str, float]] = []
    seen: set[str] = set()
    for raw in boxes or []:
        if not isinstance(raw, dict):
            continue
        box = _box(raw)
        if box["width"] < 8 or box["height"] < 8:
            continue
        key = (f"{round(box['x'] / quantum) * quantum},"
               f"{round(box['y'] / quantum) * quantum}")
        if key in seen:
            continue
        seen.add(key)
        out.append(box)
    return out


def sort_reading_order(boxes: list[dict[str, Any]],
                       y_tolerance: float = 12) -> list[dict[str, Any]]:
    """Left-to-right, top-to-bottom — the order overlaid numbers use."""
    rows = []
    ordered = sorted(list(boxes), key=lambda b: (b["y"], b["x"]))
    for box in ordered:
        for row in rows:
            if abs(row[0]["y"] - box["y"]) <= y_tolerance:
                row.append(box)
                break
        else:
            rows.append([box])
    out = []
    for row in rows:
        out.extend(sorted(row, key=lambda b: b["x"]))
    return out


def _intersection_area(a: dict[str, float], b: dict[str, float]) -> float:
    w = max(0.0, min(a["x"] + a["width"], b["x"] + b["width"])
            - max(a["x"], b["x"]))
    h = max(0.0, min(a["y"] + a["height"], b["y"] + b["height"])
            - max(a["y"], b["y"]))
    return w * h


def collapse_nested_boxes(boxes: list[dict[str, Any]] | None,
                          overlap: float = 0.8,
                          min_area_ratio: float = 0.7
                          ) -> list[dict[str, Any]]:
    """Drop inset selected-state inner boxes inside a larger tile.

    After a Verify click, selected cells expose a ~4px-inset wrapper;
    keeping both numbers a 3x3 as 12. Only near-same-size wrappers go —
    a photo tile inside a much larger widget frame is left alone.
    """
    lst = sort_reading_order(dedupe_boxes(boxes))
    lst.sort(key=lambda b: b["width"] * b["height"], reverse=True)
    kept: list[dict[str, Any]] = []
    for box in lst:
        area = max(1.0, box["width"] * box["height"])
        nested = False
        for outer in kept:
            outer_area = max(1.0, outer["width"] * outer["height"])
            if area / outer_area < min_area_ratio:
                continue
            if _intersection_area(box, outer) / area >= overlap:
                nested = True
                break
        if not nested:
            kept.append(box)
    return sort_reading_order(kept)


def cluster_similar_boxes(boxes: list[dict[str, Any]] | None,
                          min_count: int = 3,
                          size_slack: float = 14
                          ) -> list[dict[str, Any]]:
    """Keep the largest cluster of similarly sized rectangles.

    Grid tiles share a size; headers, prompts, widget chrome do not.
    """
    usable = [b for b in dedupe_boxes(boxes)
              if 24 <= b["width"] <= 480 and 24 <= b["height"] <= 480]
    best: list[dict[str, Any]] = []
    for seed in usable:
        group = [b for b in usable
                 if abs(b["width"] - seed["width"]) <= size_slack
                 and abs(b["height"] - seed["height"]) <= size_slack]
        if len(group) > len(best):
            best = group
    if len(best) < min_count:
        return []
    collapsed = collapse_nested_boxes(best)
    if len(collapsed) < min_count:
        return []
    return sort_reading_order(collapsed)


def grid_dims(tiles: list[dict[str, Any]] | None,
              y_tol: float = 12, x_tol: float = 12) -> dict[str, int]:
    """Row/col count from tile positions (their gridFromTiles)."""
    rows: list[float] = []
    cols: list[float] = []
    for t in tiles or []:
        b = t.get("bounds", t) if isinstance(t, dict) else {}
        try:
            y, x = float(b.get("y")), float(b.get("x"))
        except (ValueError, TypeError, AttributeError):
            continue
        if all(abs(r - y) > y_tol for r in rows):
            rows.append(y)
        if all(abs(c - x) > x_tol for c in cols):
            cols.append(x)
    return {"rows": len(rows), "cols": len(cols)}


def infer_grid_tiles(box: dict[str, Any], cols: int = 3,
                     rows: int = 3) -> list[dict[str, Any]]:
    """Geometric fallback: split one widget rect into an RxC tile set."""
    bounds = _box(box if isinstance(box, dict) else {})
    ncols = max(2, min(5, int(cols or 3)))
    nrows = max(2, min(5, int(rows or 3)))
    if bounds["width"] < 40 or bounds["height"] < 40:
        return []
    cw, ch = bounds["width"] / ncols, bounds["height"] / nrows
    return [{"index": r * ncols + c,
             "bounds": {"x": round(bounds["x"] + c * cw),
                        "y": round(bounds["y"] + r * ch),
                        "width": round(cw), "height": round(ch)},
             "label": None}
            for r in range(nrows) for c in range(ncols)]


def is_plausible_grid(boxes: list[dict[str, Any]] | None,
                      min_tiles: int = 3, min_side: float = 48) -> bool:
    """True for a real photo grid, false for a toolbar-button row."""
    lst = list(boxes or [])
    if len(lst) < min_tiles:
        return False
    sides = [min(float(b.get("width") or 0), float(b.get("height") or 0))
             for b in lst if isinstance(b, dict)]
    if sum(1 for s in sides if s >= min_side) < min_tiles:
        return False
    if len(lst) >= 6:
        return True
    dims = grid_dims(lst)
    if dims["rows"] == 1 and dims["cols"] >= 3 and max(sides) < 72:
        return False
    return True


OVERLAY_PIXEL_BUDGET = 518400  # 720^2: Kraken v1.2/Abyss serving band
OVERLAY_LABEL_COLOR = (255, 0, 0)  # red labels, top-right (their convention)


def render_numbered_overlay(png: bytes, tiles: list[dict[str, Any]],
                            frame_box: dict[str, Any] | None = None
                            ) -> bytes | None:
    """Draw 1-based tile numbers (red, top-right) and resize to budget.

    ``tiles`` carry viewport-px ``box`` dicts (from discovery);
    ``frame_box`` is the viewport-px box of the screenshot ``png`` itself
    (bframe element box), used to translate tiles into PNG pixels via the
    PNG/box scale ratio. ``None`` frame_box means tile boxes are already
    PNG pixels. Labels are 1-based — the model reads them, never invents
    a numbering (their 4x4-without-overlay scores zero). Returns PNG
    bytes sized to exactly OVERLAY_PIXEL_BUDGET area, or None on failure.
    """
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None
    try:
        import io as _io

        img = Image.open(_io.BytesIO(png)).convert("RGB")
        pw, ph = img.size
        sx = sy = 1.0
        ox = oy = 0.0
        if frame_box:
            try:
                fw = max(1.0, float(frame_box.get("width", 0)))
                fh = max(1.0, float(frame_box.get("height", 0)))
                ox, oy = float(frame_box.get("x", 0)), float(
                    frame_box.get("y", 0))
                sx, sy = pw / fw, ph / fh
            except (ValueError, TypeError):
                pass
        draw = ImageDraw.Draw(img)
        for tile in tiles or []:
            try:
                idx = int(tile.get("index", 0))
                b = tile.get("box", {})
                x = (float(b.get("x", 0)) - ox) * sx
                y = (float(b.get("y", 0)) - oy) * sy
                w = float(b.get("width", 0)) * sx
                h = float(b.get("height", 0)) * sy
            except (ValueError, TypeError, AttributeError):
                continue
            label = str(idx + 1)  # 1-based: the model reads these
            tx = max(0, min(pw - 12, x + w - 14))
            ty = max(0, min(ph - 12, y + 2))
            draw.rectangle([tx - 1, ty - 1, tx + 11, ty + 11],
                           fill=(0, 0, 0))
            draw.text((tx, ty), label, fill=OVERLAY_LABEL_COLOR)
        # Resize to the serving band (flat 720^2): mild extrapolation
        # over real pixels beats native-grid drift (their measurement).
        import math as _math

        scale = _math.sqrt(OVERLAY_PIXEL_BUDGET / max(1, pw * ph))
        nw, nh = max(1, round(pw * scale)), max(1, round(ph * scale))
        if (nw, nh) != (pw, ph):
            img = img.resize((nw, nh))
        out = _io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001
        return None


def union_clip(boxes: list[dict[str, Any]] | None, pad: float = 12,
               prompt_pad: float = 72,
               viewport: dict[str, Any] | None = None
               ) -> dict[str, float] | None:
    """Tight screenshot clip over tiles + a strip above for the prompt."""
    lst = dedupe_boxes(boxes)
    if not lst:
        return None
    import math as _math

    x = max(0, _math.floor(min(b["x"] for b in lst) - pad))
    y = max(0, _math.floor(min(b["y"] for b in lst) - pad - prompt_pad))
    right = _math.ceil(max(b["x"] + b["width"] for b in lst) + pad)
    bottom = _math.ceil(max(b["y"] + b["height"] for b in lst) + pad)
    if isinstance(viewport, dict):
        try:
            vw, vh = float(viewport.get("width")), float(viewport.get("height"))
        except (ValueError, TypeError):
            vw = vh = 0
        if vw > 0:
            x, right = min(x, max(0, vw - 1)), min(right, vw)
        if vh > 0:
            y, bottom = min(y, max(0, vh - 1)), min(bottom, vh)
    return {"x": float(x), "y": float(y),
            "width": float(max(1, right - x)),
            "height": float(max(1, bottom - y))}
