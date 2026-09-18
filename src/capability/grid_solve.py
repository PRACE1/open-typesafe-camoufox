"""grid_solve.py — vision grid solver: discover → overlay → Kraken → click → verify.

Vision library: https://github.com/JWriter20/CaptchaKraken

End-to-end image-grid flow for an OPEN follow-up popup (never the
anchor checkbox — clicking that closes the popup). One round is:
tile discovery (``captcha_ocr.discover_grid_tiles``) → numbered overlay
(``tiles.render_numbered_overlay``) → hosted vision round
(``backends.kraken.solve_grid``) → click returned tiles natively →
verify-button readiness gate (``tiles.is_verify_ready`` — never click a
Skip-labeled button) → cleared poll (popup gone/hidden or token
present). Dynamic refresh re-runs discovery each round; bounded by
``MAX_ROUNDS`` (mirrors the 5-response billing cap pressure).

Never raises; every outcome is a structured ``GridSolveResult`` with
the per-round trail in ``logs`` and the packed ``captchakraken:`` line
for the runner audit. No clicks when the backend is unavailable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, Field

from .logging_utils import log as _base_log


def log(msg: str, tag: str = "captchakraken:grid") -> None:
    """Hierarchical domain logger: captchakraken:grid[:stage].

    Named after the actual library doing the work (CaptchaKraken
    hosted vision) so the audit shows which backend acted.

    Sub-tags name the exact stage
    (``[capability:captchakraken:grid:click]``) so the audit shows which
    solver did what — the basis for comparing route effectiveness
    across runs.
    """
    _base_log(msg, tag=tag)

RESULT_MARKER = "captchakraken:"
MAX_ROUNDS = 3
VERIFY_POLL_S = 8.0
POLL_INTERVAL_S = 0.5


class GridSolveResult(BaseModel):
    """Structured outcome of one vision grid solve (possibly multi-round)."""

    model_config = {"extra": "ignore"}

    available: bool = False
    success: bool = False
    rounds: int = 0
    tiles_clicked: list[int] = Field(default_factory=list)
    cleared_how: str = ""
    token_len: int = 0
    elapsed_ms: int = 0
    error: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    logs: list[str] = Field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        """Compact dict for JSONL audit (no image bytes, no keys)."""
        return {
            "available": self.available,
            "success": self.success,
            "rounds": self.rounds,
            "tiles_clicked": list(self.tiles_clicked),
            "cleared_how": self.cleared_how,
            "token_len": self.token_len,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "data": self.data,
            "logs": list(self.logs),
        }


def pack_result_line(idx: int, result: GridSolveResult) -> str:
    """Single-line action result carrying the structured grid payload."""
    import json as _json

    payload = _json.dumps(result.to_record(), ensure_ascii=False)
    verdict = "solved" if result.success else "unsolved"
    return (f"element #{idx} {RESULT_MARKER}{payload} "
            f"{verdict} rounds={result.rounds} "
            f"awaiting JEV review (no credential dispatch)")


def unpack_result_line(line: str) -> dict | None:
    """Extract the structured grid payload from a packed result line."""
    import json as _json

    try:
        start = line.index(RESULT_MARKER) + len(RESULT_MARKER)
    except ValueError:
        return None
    tail = line[start:].strip()
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


async def _read_instruction(platform) -> str:
    """Grid instruction text from the bframe DOM (fail-soft "")."""
    from .shield_solve import RECAPTCHA_BFRAME_SEL
    from .tiles import CHALLENGE_INSTRUCTION_SELECTORS

    try:
        frame = platform.page.frame_locator(RECAPTCHA_BFRAME_SEL).first
    except Exception:  # noqa: BLE001
        return ""
    for sel in CHALLENGE_INSTRUCTION_SELECTORS:
        try:
            loc = frame.locator(sel).first
            if await asyncio.wait_for(loc.count(), timeout=3.0) == 0:
                continue
            text = await asyncio.wait_for(loc.inner_text(), timeout=5.0)
        except Exception:  # noqa: BLE001
            continue
        text = " ".join(str(text or "").split())
        if text:
            return text[:300]
    return ""


async def _capture_bframe(platform) -> tuple[bytes | None, dict | None]:
    """bframe element screenshot + its viewport box (for overlay mapping)."""
    from .shield_solve import RECAPTCHA_BFRAME_SEL

    try:
        loc = platform.page.locator(RECAPTCHA_BFRAME_SEL).first
        if await asyncio.wait_for(loc.count(), timeout=5.0) == 0:
            return None, None
        png = await asyncio.wait_for(
            loc.screenshot(timeout=10000), timeout=12.0)
        try:
            raw_box = await asyncio.wait_for(loc.bounding_box(),
                                             timeout=5.0)
            box = dict(raw_box) if raw_box else None
        except Exception:  # noqa: BLE001
            box = None
    except Exception as exc:  # noqa: BLE001
        log(f"bframe capture failed: {exc}", tag="captchakraken:grid:capture")
        return None, None
    return png, box


async def _click_tiles(platform, tiles: list[dict[str, Any]],
                       indexes: list[int], logs: list[str]) -> list[int]:
    """Click returned tiles by reading-order index; returns clicked ones.

    Each click rides the humanized trajectory stack (cursory-recorded
    path onto the tile center, then the locator click) — tiles get the
    same human motion as every other dispatch, never a bare teleport.
    """
    from .human_move import cursory_move

    by_index = {int(t.get("index", -1)): t for t in tiles or []}
    clicked: list[int] = []
    for i in indexes:
        tile = by_index.get(int(i))
        handle = (tile or {}).get("locator")
        if handle is None:
            logs.append(f"tile #{i}: no locator (map drifted) — skipped")
            continue
        box = (tile or {}).get("box", {}) or {}
        try:
            cx = float(box.get("x", 0)) + float(box.get("width", 0)) / 2
            cy = float(box.get("y", 0)) + float(box.get("height", 0)) / 2
        except (ValueError, TypeError):
            cx, cy = 0.0, 0.0
        try:
            start = getattr(platform, "_last_cursor_pos", None) or (cx, cy)
            page = platform.page
            moved = await cursory_move(page, start, (cx, cy), vp=(
                page.viewport_size or {"width": 1280, "height": 800}))
            logs.append(f"tile #{i}: humanized move "
                        f"{'ok' if moved else 'failed, direct click'}")
            log(f"tile={i} move={'ok' if moved else 'direct-click'}",
                tag="captchakraken:grid:click")
        except Exception as exc:  # noqa: BLE001
            logs.append(f"tile #{i}: move failed "
                        f"({exc.__class__.__name__}), direct click")
        try:
            await asyncio.wait_for(handle.click(timeout=5000), timeout=8.0)
            clicked.append(int(i))
            logs.append(f"tile #{i}: clicked")
            log(f"tile={i} result=success", tag="captchakraken:grid:click")
            try:
                platform._last_cursor_pos = (cx, cy)
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            logs.append(f"tile #{i}: click failed ({exc.__class__.__name__})")
    return clicked


async def _verify_button_label(platform) -> str:
    """Current verify-button label in the bframe ("" when absent)."""
    from .shield_solve import RECAPTCHA_BFRAME_SEL
    from .tiles import VERIFY_BUTTON_SELECTORS

    try:
        frame = platform.page.frame_locator(RECAPTCHA_BFRAME_SEL).first
    except Exception:  # noqa: BLE001
        return ""
    for sel in VERIFY_BUTTON_SELECTORS:
        try:
            loc = frame.locator(sel).first
            if await asyncio.wait_for(loc.count(), timeout=3.0) == 0:
                continue
            text = await asyncio.wait_for(loc.inner_text(), timeout=5.0)
        except Exception:  # noqa: BLE001
            continue
        text = " ".join(str(text or "").split())
        if text:
            return text[:60]
    return ""


async def _click_verify(platform) -> bool:
    """Click the bframe verify/submit button. True if dispatched."""
    from .shield_solve import RECAPTCHA_BFRAME_SEL
    from .tiles import VERIFY_BUTTON_SELECTORS

    try:
        frame = platform.page.frame_locator(RECAPTCHA_BFRAME_SEL).first
    except Exception:  # noqa: BLE001
        return False
    for sel in VERIFY_BUTTON_SELECTORS:
        try:
            loc = frame.locator(sel).first
            if await asyncio.wait_for(loc.count(), timeout=3.0) == 0:
                continue
            await asyncio.wait_for(loc.click(timeout=5000), timeout=8.0)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _is_cleared(platform) -> tuple[bool, str, int]:
    """(cleared, how, token_len): popup gone/hidden or token present."""
    from .shield_solve import (
        RECAPTCHA_TOKEN_SEL,
        _read_token_len,
        detect_image_followup,
    )

    try:
        token_len = await _read_token_len(platform.page, RECAPTCHA_TOKEN_SEL)
    except Exception:  # noqa: BLE001
        token_len = 0
    if token_len:
        return True, "token", token_len
    try:
        page_text = ""
        try:
            from ..perception import get_page_text

            page_text = await get_page_text(platform, limit=800)
        except Exception:  # noqa: BLE001
            pass
        open_now, _via = await detect_image_followup(platform, page_text)
    except Exception:  # noqa: BLE001
        open_now = True
    if not open_now:
        return True, "popup-closed", 0
    return False, "", 0


async def run_vision_grid(platform, instruction: str = "",
                          session_id: str = "",
                          max_rounds: int = MAX_ROUNDS
                          ) -> GridSolveResult:
    """Full vision solve of the open grid popup. Never raises.

    Needs the follow-up OPEN (caller checks ``detect_image_followup``);
    never touches the anchor checkbox. Each round re-discovers tiles
    (dynamic refresh replaces them), solves one overlay round, clicks,
    then gates the verify button on ``is_verify_ready`` before polling
    cleared. Stops on cleared, on backend refusal, or at max rounds.
    """
    from .backends import kraken as _kraken
    from .captcha_ocr import discover_grid_tiles
    from .tiles import (
        grid_dims,
        is_verify_ready,
        render_numbered_overlay,
    )

    t0 = time.monotonic()
    elapsed = lambda: int((time.monotonic() - t0) * 1000)
    logs = ["vision grid solve start (hosted Kraken, never local models)"]
    if not _kraken.is_available():
        logs.append("unavailable: CAPTCHA_KRAKEN_API_KEY missing")
        return GridSolveResult(logs=logs, elapsed_ms=elapsed(),
                               error="CAPTCHA_KRAKEN_API_KEY missing")
    session = session_id or _kraken.new_session_id()
    logs.append(f"billing session {(session or '')[:12]}")
    if not (instruction or "").strip():
        instruction = await _read_instruction(platform)
    logs.append(f"instruction: {(instruction or '(unread)')[:100]}")
    tiles_clicked: list[int] = []
    prev_label = ""
    had_selection = False
    for round_no in range(1, max(1, max_rounds) + 1):
        logs.append(f"round {round_no}: discovering tiles")
        tiles = await discover_grid_tiles(platform)
        if not tiles:
            logs.append("round {0}: no tiles discovered — stop".format(round_no))
            return GridSolveResult(
                available=True, rounds=round_no - 1,
                tiles_clicked=tiles_clicked, logs=logs,
                elapsed_ms=elapsed(),
                error="no grid tiles discovered (popup changed?)")
        dims = grid_dims([{"x": t["box"]["x"], "y": t["box"]["y"]}
                          for t in tiles])
        rows, cols = max(1, dims["rows"]), max(1, dims["cols"])
        logs.append(f"round {round_no}: {len(tiles)} tiles {rows}x{cols}")
        png, frame_box = await _capture_bframe(platform)
        if not png:
            logs.append(f"round {round_no}: bframe capture failed — stop")
            return GridSolveResult(
                available=True, rounds=round_no - 1,
                tiles_clicked=tiles_clicked, logs=logs,
                elapsed_ms=elapsed(), error="bframe capture failed")
        overlay = render_numbered_overlay(png, tiles, frame_box)
        if not overlay:
            logs.append(f"round {round_no}: overlay render failed — stop")
            return GridSolveResult(
                available=True, rounds=round_no - 1,
                tiles_clicked=tiles_clicked, logs=logs,
                elapsed_ms=elapsed(), error="overlay render failed")
        indexes, usage, klogs = await _kraken.solve_grid(
            overlay, instruction, rows, cols, session)
        logs.extend(klogs)
        if usage.get("error"):
            logs.append(f"round {round_no}: vision refused "
                        f"({usage.get('code')}) — stop")
            return GridSolveResult(
                available=True, rounds=round_no - 1,
                tiles_clicked=tiles_clicked, logs=logs,
                elapsed_ms=elapsed(),
                error=f"vision refused: {usage.get('code')}",
                data={"usage": usage})
        clicked = await _click_tiles(platform, tiles, indexes, logs)
        tiles_clicked.extend(c for c in clicked if c not in tiles_clicked)
        had_selection = had_selection or bool(clicked)
        label = await _verify_button_label(platform)
        logs.append(f"round {round_no}: verify button reads {label!r:.30}")
        if is_verify_ready(label, prev_label, had_selection):
            logs.append(f"round {round_no}: verify ready — clicking")
            if await _click_verify(platform):
                cleared, how, token_len = await _poll_cleared(platform)
                if cleared:
                    logs.append(f"CLEARED via {how} "
                                f"(token_len={token_len})")
                    return GridSolveResult(
                        available=True, success=True, rounds=round_no,
                        tiles_clicked=tiles_clicked, cleared_how=how,
                        token_len=token_len, elapsed_ms=elapsed(),
                        data={"usage": usage}, logs=logs)
                logs.append(f"round {round_no}: verify clicked, "
                            f"still open — next round")
            else:
                logs.append(f"round {round_no}: verify click failed")
        else:
            logs.append(f"round {round_no}: verify NOT ready "
                        f"(skip-labeled?) — next round")
        prev_label = label
    logs.append(f"rounds exhausted ({max_rounds}) — popup left open")
    return GridSolveResult(
        available=True, rounds=max_rounds, tiles_clicked=tiles_clicked,
        logs=logs, elapsed_ms=elapsed(),
        error="rounds exhausted without clear")


async def _poll_cleared(platform,
                        timeout_s: float = VERIFY_POLL_S) -> tuple[bool, str, int]:
    """Poll cleared until deadline (lengths only, never values)."""
    end = time.monotonic() + max(1.0, timeout_s)
    last: tuple[bool, str, int] = (False, "", 0)
    while time.monotonic() < end:
        last = await _is_cleared(platform)
        if last[0]:
            return last
        await asyncio.sleep(POLL_INTERVAL_S)
    return last
