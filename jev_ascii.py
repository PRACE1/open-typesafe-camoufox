"""
Braille ASCII-art encoder — the deterministic version of the trick DeepSeek
improvised through prime-agent's attach-image fallback path.

Math ported from prime-agent `scripts/render-logo.py` (`--style braille`):
  pixels -> composite on black -> luminance ("L") -> threshold per subpixel
  -> 2x4 dot grid per character cell -> chr(0x2800 + mask) (U+2800-U+28FF).

Differences from render-logo.py (deliberate, documented):
  - Input is an already-raster screenshot (PIL Image), so the cairosvg/SVG
    raster step is skipped. render-logo.py only ever rendered its logo SVG.
  - Default threshold 128 instead of 96: screenshots are mid-tone photos,
    not a light logo on transparency. Exposed as a parameter mirroring
    render-logo.py's `--threshold`.
  - No cairo/system deps: screenshots need no SVG rasterization.
"""

from __future__ import annotations

# (dx, dy, bit) — identical table to render-logo.py's braille branch.
# A Braille cell is a 2x4 grid; terminal cells are ~2x taller than wide,
# so these subpixels are approximately square and aspect is preserved
# without vertical rescaling.
_DOT_BITS = (
    (0, 0, 0),
    (0, 1, 1),
    (0, 2, 2),
    (1, 0, 3),
    (1, 1, 4),
    (1, 2, 5),
    (0, 3, 6),
    (1, 3, 7),
)


def frame_to_braille(
    image,
    *,
    width_cols: int = 80,
    threshold: int = 128,
) -> tuple[str, int, int]:
    """Encode a PIL image as braille text.

    Args:
        image: PIL Image (any mode; converted internally).
        width_cols: output text width in characters. Each char covers
            2x4 pixels, so the image is resized to (width_cols*2) px wide.
        threshold: luminance cutoff 0-255; raise for thinner strokes
            (render-logo.py default: 96).

    Returns:
        (text, cols, rows): stripped text plus its grid dimensions.
    """
    from PIL import Image

    img = image.convert("RGBA")
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    bg.paste(img, (0, 0), img)
    img = bg.convert("L")

    target_w = max(2, width_cols * 2)
    scale = target_w / max(1, img.size[0])
    target_h = max(4, round(img.size[1] * scale))
    # Pad to whole 2x4 cells.
    pad_w = target_w + (-target_w % 2)
    pad_h = target_h + (-target_h % 4)
    img = img.resize((target_w, target_h), Image.LANCZOS)
    if (pad_w, pad_h) != (target_w, target_h):
        padded = Image.new("L", (pad_w, pad_h), 0)
        padded.paste(img, (0, 0))
        img = padded

    px = img.load()
    rows: list[str] = []
    for y in range(0, img.size[1], 4):
        row: list[str] = []
        for x in range(0, img.size[0], 2):
            mask = 0
            for dx, dy, bit in _DOT_BITS:
                if px[x + dx, y + dy] > threshold:
                    mask |= 1 << bit
            row.append(chr(0x2800 + mask) if mask else " ")
        rows.append("".join(row).rstrip())

    while rows and not rows[0].strip():
        rows.pop(0)
    while rows and not rows[-1].strip():
        rows.pop()
    cols = max((len(r) for r in rows), default=0)
    return "\n".join(rows), cols, len(rows)


def grid_span(cell: int, grid: int, cols: int, rows: int) -> str:
    """Human-readable char-span of a Choice grid cell, for criteria text."""
    gx, gy = cell % grid, cell // grid
    c0, c1 = round(gx * cols / grid), round((gx + 1) * cols / grid)
    r0, r1 = round(gy * rows / grid), round((gy + 1) * rows / grid)
    return f"cols {c0}-{c1}, rows {r0}-{r1} (x~{(gx+0.5)/grid:.2f}, y~{(gy+0.5)/grid:.2f})"
