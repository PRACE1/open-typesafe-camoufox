"""Grid-solve tests — vision flow orchestration (offline fakes).

The Kraken transport is stubbed at ``backends.kraken.solve_grid``;
tile discovery, capture, and verify gates are stubbed at the
``grid_solve`` module boundary. No key, no network, no browser.
"""

import asyncio
import io

from src.capability import grid_solve as gs


def _run(coro):
    return asyncio.run(coro)


def _png(w=300, h=300):
    from PIL import Image

    img = Image.new("RGB", (w, h), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _TileHandle:
    def __init__(self):
        self.clicked = 0

    async def click(self, timeout=None):
        self.clicked += 1


def _tiles(n=9):
    return [{"index": i,
             "box": {"x": 10.0 + (i % 3) * 100, "y": 20.0 + (i // 3) * 100,
                     "width": 100.0, "height": 100.0},
             "locator": _TileHandle()} for i in range(n)]


class _Plat:
    page = object()


def _patch_flow(monkeypatch, *, indexes=(1, 7), cleared=True):
    import src.capability.captcha_ocr as co

    async def _discover(platform):
        return _tiles()

    async def _capture(platform):
        return _png(), {"x": 0.0, "y": 0.0, "width": 320.0,
                        "height": 320.0}

    async def _solve(overlay, instruction, rows, cols, session="",
                     timeout_s=60.0):
        assert rows == 3 and cols == 3
        assert session.startswith("otc-") or session
        return list(indexes), {"model": "abyss-grid"}, ["round ok"]

    async def _label(platform):
        return "Verify"

    async def _click_verify(platform):
        return True

    async def _cleared(platform):
        return (cleared, "token" if cleared else "", 64 if cleared else 0)

    monkeypatch.setattr(co, "discover_grid_tiles", _discover)
    monkeypatch.setattr(gs, "_capture_bframe", _capture)
    monkeypatch.setattr(gs, "_verify_button_label", _label)
    monkeypatch.setattr(gs, "_click_verify", _click_verify)
    monkeypatch.setattr(gs, "_is_cleared", _cleared)
    import src.capability.backends.kraken as kk

    async def _kraken_solve(overlay_png, instruction, rows, cols,
                            session_id="", timeout_s=60.0):
        return await _solve(overlay_png, instruction, rows, cols,
                            session_id, timeout_s)

    monkeypatch.setattr(kk, "solve_grid", _kraken_solve)
    monkeypatch.setattr(kk, "is_available", lambda: True)


def test_vision_grid_solves_and_packs(monkeypatch):
    _patch_flow(monkeypatch)
    res = _run(gs.run_vision_grid(_Plat(), instruction="bicycles",
                                  session_id="otc-test"))
    assert res.success is True and res.rounds == 1
    assert res.tiles_clicked == [1, 7]
    assert res.cleared_how == "token" and res.token_len == 64
    assert any("CLEARED" in line for line in res.logs)
    line = gs.pack_result_line(5, res)
    assert gs.RESULT_MARKER in line and "solved" in line
    obj = gs.unpack_result_line(line)
    assert obj is not None and obj["success"] is True
    assert obj["tiles_clicked"] == [1, 7]


def test_vision_grid_unavailable_without_key(monkeypatch):
    import src.capability.backends.kraken as kk

    monkeypatch.setattr(kk, "is_available", lambda: False)
    res = _run(gs.run_vision_grid(_Plat()))
    assert res.success is False and "CAPTCHA_KRAKEN_API_KEY" in (
        res.error or "")
    assert res.rounds == 0  # no clicks dispatched


def test_vision_grid_backend_refusal_leaves_open(monkeypatch):
    _patch_flow(monkeypatch)
    import src.capability.backends.kraken as kk

    async def _refuse(overlay_png, instruction, rows, cols,
                      session_id="", timeout_s=60.0):
        return [], {"error": "rate-limited", "code": "rate_limited"}, []

    monkeypatch.setattr(kk, "solve_grid", _refuse)
    res = _run(gs.run_vision_grid(_Plat(), session_id="otc-test"))
    assert res.success is False and "rate_limited" in (res.error or "")
    assert res.tiles_clicked == []  # nothing clicked on refusal


def test_vision_grid_no_tiles_stops(monkeypatch):
    _patch_flow(monkeypatch)
    import src.capability.captcha_ocr as co

    async def _empty(platform):
        return []

    monkeypatch.setattr(co, "discover_grid_tiles", _empty)
    res = _run(gs.run_vision_grid(_Plat(), session_id="otc-test"))
    assert res.success is False and "no grid tiles" in (res.error or "")


def test_unpack_rejects_plain_lines():
    assert gs.unpack_result_line("element #5 scroll+circle+click") is None
    assert gs.unpack_result_line("error: element #5 target gone") is None


class _MoveMouse:
    def __init__(self):
        self.moves = []

    async def move(self, x, y, **kwargs):
        self.moves.append((x, y))


class _MovePage:
    def __init__(self):
        self.mouse = _MoveMouse()
        self.viewport_size = {"width": 1000, "height": 800}


class _MovePlat:
    def __init__(self):
        self.page = _MovePage()
        self._last_cursor_pos = (0.0, 0.0)


def test_tile_clicks_ride_humanized_trajectory():
    tiles = _tiles(3)
    logs: list[str] = []
    plat = _MovePlat()
    clicked = asyncio.run(gs._click_tiles(plat, tiles, [0, 2], logs))
    assert clicked == [0, 2]
    # recorded-human moves dispatched before each locator click
    assert len(plat.page.mouse.moves) >= 2
    assert plat._last_cursor_pos != (0.0, 0.0)
    assert any("humanized move ok" in line for line in logs)


def test_hierarchical_log_prefix(capsys):
    gs.log("probe", tag="captchakraken:grid:click")
    out = capsys.readouterr().out
    assert out.startswith("[capability:captchakraken:grid:click]")
