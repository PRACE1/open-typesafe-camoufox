"""Braille encoder tests — deterministic prime-agent math, grid grounding."""

from jev_ascii import frame_to_braille, grid_span
from jev_client import GRID, _decode_cell


def _test_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (160, 160), (0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([100, 20, 140, 60], fill=(255, 255, 255))  # bright block, right-top
    return img


def test_black_frame_renders_blank():
    from PIL import Image

    text, cols, rows = frame_to_braille(Image.new("RGB", (160, 160), (0, 0, 0)))
    assert text == ""
    assert rows == 0


def test_white_block_renders_filled_cells():
    text, cols, rows = frame_to_braille(_test_image(), width_cols=40, threshold=128)
    assert rows > 0 and cols > 0
    filled = [c for c in text if c != " " and c != "\n"]
    assert filled, "bright block must produce non-blank braille"
    assert all("\u2800" <= c <= "\u28ff" for c in filled)


def test_threshold_gates_output():
    img = _test_image()
    low, _, _ = frame_to_braille(img, width_cols=40, threshold=1)
    high, _, _ = frame_to_braille(img, width_cols=40, threshold=255)
    assert len(low) >= len(high)


def test_grid_span_names_char_ranges():
    s = grid_span(27, GRID, 80, 40)
    assert "cols" in s and "rows" in s and "x~" in s


def test_choice_cardinality_under_cap():
    assert GRID * GRID == 64 <= 255


def test_decode_corners():
    assert _decode_cell(0) == (0.0625, 0.0625)
    x, y = _decode_cell(63)
    assert abs(x - 0.9375) < 1e-9 and abs(y - 0.9375) < 1e-9
