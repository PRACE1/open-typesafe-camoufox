"""screenx_patch tests — rule parity + frame wiring (offline).

The JS override itself is executed for real by the node stress harness
(see agent session notes); these tests pin the Python mirror, the
frame-selection/install wiring with faked frames, and the probe
verdict. Token values never appear anywhere here.
"""

import asyncio

import pytest

from src.capability import screenx_patch as sx
from src.capability.screenx_patch import (
    PATCHED_MAX_X,
    PATCHED_MAX_Y,
    PATCHED_MIN_X,
    PATCHED_MIN_Y,
    SCREENX_PATCH_JS,
    SCREENX_PROBE_JS,
    install_screenx_patch,
    needs_patch,
    probe_passes,
    probe_screenx,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeFrame:
    def __init__(self, url, evaluate=None):
        self.url = url
        self.evaluated = []
        self._evaluate = evaluate

    async def evaluate(self, js):
        self.evaluated.append(js)
        if self._evaluate is not None:
            return self._evaluate(js)
        return "ok"


class _FakePage:
    def __init__(self, frames):
        self.frames = frames


def _probe_ok():
    return {
        "patched": True,
        "synthetic_small": {"screenX": 300, "screenY": 150},
        "genuine_large": {"screenX": 500, "screenY": 300},
    }


def test_needs_patch_cdp_signature():
    # CDP synthetic: screenX == clientX, small.
    assert needs_patch(50, 50, in_iframe=True) is True
    assert needs_patch(50, 50, in_iframe=False) is True


def test_needs_patch_widget_offset():
    # Linux XTEST style: small but != clientX — patched only in iframe.
    assert needs_patch(70, 200, in_iframe=True) is True
    assert needs_patch(70, 200, in_iframe=False) is False


def test_needs_patch_genuine_passthrough():
    assert needs_patch(500, 480, in_iframe=True) is False
    assert needs_patch(800, 50, in_iframe=False) is False


def test_needs_patch_non_finite():
    assert needs_patch(float("nan"), 10, in_iframe=False) is True
    assert needs_patch(float("inf"), 10, in_iframe=False) is True
    assert needs_patch("bogus", 10, in_iframe=False) is True


def test_js_carries_upstream_anchors():
    # Guards against drift from bypass/ext/script.js semantics.
    assert "needsPatch" in SCREENX_PATCH_JS
    assert "240, 960" in SCREENX_PATCH_JS
    assert "80, 420" in SCREENX_PATCH_JS
    assert "MouseEvent.prototype" in SCREENX_PATCH_JS
    assert "data-cf-ts-click" in SCREENX_PATCH_JS
    assert "screenX" in SCREENX_PROBE_JS


def test_install_only_touches_challenge_frames():
    top = _FakeFrame("https://example.com/")
    cf = _FakeFrame("https://challenges.cloudflare.com/turnstile/v0/x")
    page = _FakePage([top, cf])
    assert _run(install_screenx_patch(page)) == 1
    assert top.evaluated == []
    assert len(cf.evaluated) == 1


def test_install_fail_soft():
    class _Boom:
        frames = None

    assert _run(install_screenx_patch(_Boom())) == 0
    assert _run(probe_screenx(_Boom())) == []


def test_probe_passes():
    assert probe_passes(_probe_ok()) is True
    bad = _probe_ok()
    bad["synthetic_small"] = {"screenX": 30, "screenY": 20}
    assert probe_passes(bad) is False
    tampered = _probe_ok()
    tampered["genuine_large"] = {"screenX": 999, "screenY": 300}
    assert probe_passes(tampered) is False


def test_probe_readings_shape():
    cf = _FakeFrame("https://challenges.cloudflare.com/turnstile/v0/x",
                    evaluate=lambda js: _probe_ok())
    out = _run(probe_screenx(_FakePage([cf])))
    assert len(out) == 1
    assert out[0]["frame_url"].startswith("https://challenges.cloudflare.com")
    assert probe_passes(out[0]) is True


def test_bounds_sane():
    assert PATCHED_MIN_X == 240
    assert PATCHED_MIN_Y == 80
    assert PATCHED_MAX_X > PATCHED_MIN_X and PATCHED_MAX_Y > PATCHED_MIN_Y


@pytest.mark.skip(reason="live browser only — run headed against a Turnstile demo page")
def test_live_turnstile_probe():
    raise NotImplementedError
