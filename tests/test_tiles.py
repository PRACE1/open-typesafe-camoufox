"""Tile geometry tests — pure box math, no browser (offline)."""

from src.capability.tiles import (
    cluster_similar_boxes,
    collapse_nested_boxes,
    dedupe_boxes,
    grid_dims,
    infer_grid_tiles,
    is_chrome_label,
    is_plausible_grid,
    is_skip_submit_label,
    is_verify_ready,
    is_verify_submit_label,
    parse_tile_indexes,
    sort_reading_order,
    union_clip,
)


def _grid3x3(cell=100, ox=10, oy=20):
    return [{"x": ox + c * cell, "y": oy + r * cell,
             "width": cell, "height": cell}
            for r in range(3) for c in range(3)]


def test_dedupe_rounds_and_drops_scraps():
    boxes = [{"x": 10.2, "y": 20.4, "width": 100, "height": 100},
             {"x": 10.4, "y": 20.6, "width": 100, "height": 100},
             {"x": 500, "y": 500, "width": 100, "height": 100},
             {"x": 5, "y": 5, "width": 4, "height": 4}]
    out = dedupe_boxes(boxes)
    assert len(out) == 2 and out[0]["x"] == 10


def test_reading_order_rows_then_cols():
    boxes = [{"x": 210, "y": 22, "width": 100, "height": 100},
             {"x": 10, "y": 20, "width": 100, "height": 100},
             {"x": 110, "y": 21, "width": 100, "height": 100}]
    out = sort_reading_order(boxes)
    assert [b["x"] for b in out] == [10, 110, 210]


def test_collapse_nested_selected_wrapper():
    outer = {"x": 10, "y": 20, "width": 100, "height": 100}
    inner = {"x": 14, "y": 24, "width": 92, "height": 92}  # ~4px inset
    assert len(collapse_nested_boxes([outer, inner])) == 1
    # A small photo inside a big frame survives (area ratio guard).
    assert len(collapse_nested_boxes(
        [outer, {"x": 20, "y": 30, "width": 30, "height": 30}])) == 2


def test_cluster_keeps_photo_grid_drops_chrome():
    boxes = _grid3x3() + [{"x": 0, "y": 0, "width": 600, "height": 40},
                          {"x": 0, "y": 600, "width": 30, "height": 12}]
    out = cluster_similar_boxes(boxes)
    assert len(out) == 9


def test_grid_dims_and_infer():
    dims = grid_dims(_grid3x3())
    assert dims == {"rows": 3, "cols": 3}
    tiles = infer_grid_tiles({"x": 0, "y": 0, "width": 300, "height": 300})
    assert len(tiles) == 9 and tiles[0]["index"] == 0
    assert tiles[8]["bounds"]["x"] == 200
    assert infer_grid_tiles({"x": 0, "y": 0, "width": 10, "height": 10}) == []


def test_plausible_grid_rejects_toolbar_row():
    assert is_plausible_grid(_grid3x3()) is True
    toolbar = [{"x": i * 40, "y": 0, "width": 32, "height": 28}
               for i in range(5)]
    assert is_plausible_grid(toolbar) is False
    assert is_plausible_grid(_grid3x3()[:2]) is False


def test_chrome_skip_verify_labels():
    assert is_chrome_label("Privacy Policy") is True
    assert is_chrome_label("traffic lights") is False
    assert is_skip_submit_label("Skip") is True
    assert is_skip_submit_label("Verify") is False
    assert is_verify_submit_label("Verify") is True
    assert is_verify_submit_label("Skip") is False
    # Unrecognized label: label-change heuristic decides.
    assert is_verify_ready("Weiter", previous_label="Skip",
                           had_selection=False) is True
    assert is_verify_ready("Weiter", previous_label="Weiter",
                           had_selection=True) is True
    assert is_verify_ready("Weiter", previous_label="Weiter",
                           had_selection=False) is False
    assert is_verify_ready("") is False


def test_parse_tile_indexes():
    assert parse_tile_indexes([0, 3, 5]) == [0, 3, 5]
    assert parse_tile_indexes("0,3,5") == [0, 3, 5]
    assert parse_tile_indexes([{"index": 2}]) == [2]
    assert parse_tile_indexes([1, 1, 99, -2, "x"]) == [1]
    assert parse_tile_indexes(None) == []


def test_union_clip_covers_tiles_plus_prompt():
    clip = union_clip(_grid3x3(), viewport={"width": 800, "height": 600})
    assert clip["x"] == 0  # clamped: 10 - 12 pad
    assert clip["width"] == 322  # 10 + 300 + 12
    assert clip["height"] == 332 - 0  # prompt strip included
    assert union_clip([]) is None
