"""Shield-solve tests — Turnstile/reCAPTCHA detect + solve + pack (offline).

Technique source: genguzzz/shield-bypass (MIT), ported to stock
async-Playwright primitives. All browser surface is faked; token
*values* never appear in records or logs (lengths only) — asserted.
"""

import asyncio

import pytest

from src.capability import shield_solve as sh
from src.capability.shield_solve import (
    RESULT_MARKER,
    ShieldDetection,
    ShieldSolveResult,
    detect_image_followup,
    detect_shield,
    pack_result_line,
    solve_shield,
    unpack_result_line,
)
from src.deps import ElementRef


def _run(coro):
    return asyncio.run(coro)


class _FakeLocator:
    def __init__(self, count=0, values=None, clicked=None, visible=True):
        self._count = count
        # Shared (not copied): polls across locator instances must observe
        # the token materializing over time, like the live page.
        self._values = values if values is not None else []
        self.clicked = clicked if clicked is not None else []
        self.visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def input_value(self, timeout=None):
        if self._values:
            return self._values.pop(0)
        return ""

    async def is_visible(self, timeout=None):
        return self.visible

    async def click(self, timeout=None, force=False, delay=None,
                    position=None):
        self.clicked.append({"timeout": timeout, "force": force,
                             "delay": delay, "position": position})

    async def element_handle(self, timeout=None):
        return None


class _FakeFrameLocator:
    def __init__(self, checkbox):
        self._checkbox = checkbox

    @property
    def first(self):
        return self

    def get_by_role(self, role):
        assert role == "checkbox"
        return self._checkbox

    def locator(self, sel):
        return self._checkbox


class _FakePage:
    def __init__(self, counts=None, token_values=None, bframe_visible=True):
        self._counts = counts or {}
        self._token_values = token_values
        self._bframe_visible = bframe_visible
        self._checkbox = _FakeLocator(count=1)
        self.frame_calls = []
        self.viewport_size = {"width": 1000, "height": 800}

    def locator(self, sel):
        for key in self._counts:
            if key in sel:
                return _FakeLocator(count=self._counts[key],
                                    values=self._token_values,
                                    visible=self._bframe_visible
                                    if "bframe" in sel else True)
        return _FakeLocator(count=0)

    def frame_locator(self, sel):
        self.frame_calls.append(sel)
        return _FakeFrameLocator(self._checkbox)

    async def evaluate(self, js, arg=None):
        raise RuntimeError("no main-world eval in tests")


class _FakePlatform:
    def __init__(self, page):
        self.page = page


def _plat(counts=None, token_values=None, bframe_visible=True):
    return _FakePlatform(_FakePage(counts, token_values, bframe_visible))


# -- detect ---------------------------------------------------------------

def test_detect_turnstile_iframe():
    det = _run(detect_shield(
        _plat({"challenges.cloudflare.com": 1}), ""))
    assert det.detected and det.challenge_type == "cf_turnstile"
    assert det.confidence == pytest.approx(0.95)


def test_detect_recaptcha_iframe():
    det = _run(detect_shield(
        _plat({"google.com/recaptcha": 1}), ""))
    assert det.detected and det.challenge_type == "recaptcha"


def test_detect_hcaptcha_iframe():
    det = _run(detect_shield(_plat({"hcaptcha": 1}), ""))
    assert det.detected and det.challenge_type == "hcaptcha"


def test_detect_cf_waf_by_text():
    det = _run(detect_shield(_plat(), "Just a moment ... verifying"))
    assert det.detected and det.challenge_type == "cf_waf"


def test_detect_nothing_clean_page():
    det = _run(detect_shield(_plat(), "ordinary article text"))
    assert det.detected is False and det.challenge_type == "none"


def test_followup_by_instruction_text():
    found, via = _run(detect_image_followup(
        _plat(), "Select all images with bicycles"))
    assert found is True and via == "page-text"


def test_followup_by_bframe_iframe():
    found, via = _run(detect_image_followup(
        _plat({"recaptcha/api2/bframe": 1}), "ordinary article text"))
    assert found is True and via == "bframe"


def test_followup_closed_clean_page():
    # Anchor iframe alone (no bframe, no instruction text) is NOT a
    # follow-up: the checkbox may still be clicked.
    found, _ = _run(detect_image_followup(
        _plat({"google.com/recaptcha": 1}), "ordinary article text"))
    assert found is False


def test_followup_hidden_bframe_is_not_open():
    # Recaptcha renders the bframe iframe hidden from page load: mere
    # presence must not block the first anchor click (seen live: zero
    # clicks dispatched because the detector fired on a hidden frame).
    found, _ = _run(detect_image_followup(
        _plat({"recaptcha/api2/bframe": 1}, bframe_visible=False),
        "ordinary article text"))
    assert found is False


# -- solve ----------------------------------------------------------------

def test_solve_turnstile_early_token_no_click():
    plat = _plat({"challenges.cloudflare.com": 1,
                  "cf-turnstile-response": 1},
                 token_values=["x" * 64])
    res = _run(solve_shield(plat, "cf_turnstile", timeout_s=5.0))
    assert res.success and res.token_len == 64
    assert res.data.get("early_token") is True
    assert plat.page._checkbox.clicked == []


def test_solve_turnstile_click_then_token():
    plat = _plat({"challenges.cloudflare.com": 1,
                  "cf-turnstile-response": 1},
                 token_values=["", "y" * 48])
    res = _run(solve_shield(plat, "cf_turnstile", timeout_s=5.0))
    assert res.success and res.token_len == 48
    assert plat.page._checkbox.clicked  # family click dispatched
    assert "challenges.cloudflare.com" in plat.page.frame_calls[0]


def test_solve_recaptcha_timeout_structured_failure(monkeypatch):
    monkeypatch.setattr(sh, "POLL_INTERVAL_S", 0.01)
    plat = _plat({"google.com/recaptcha": 1,
                  "g-recaptcha-response": 1},
                 token_values=[])
    res = _run(solve_shield(plat, "recaptcha", timeout_s=0.2))
    assert res.success is False and res.token_len == 0
    assert "image challenge" in (res.error or "")


def test_solve_unknown_family_refuses():
    res = _run(solve_shield(_plat(), "hcaptcha", timeout_s=1.0))
    assert res.success is False
    assert "no native solver" in (res.error or "")


def test_token_values_never_recorded():
    res = ShieldSolveResult(challenge_type="recaptcha", success=True,
                            token_len=120)
    rec = res.to_record()
    assert rec["token_len"] == 120
    assert "x" * 10 not in str(rec)


# -- pack/unpack ------------------------------------------------------------

def test_pack_unpack_roundtrip():
    res = ShieldSolveResult(challenge_type="cf_turnstile", success=True,
                            token_len=88, elapsed_ms=1203)
    line = pack_result_line(4, res)
    assert RESULT_MARKER in line and "solved" in line
    assert "no credential dispatch" in line
    obj = unpack_result_line(line)
    assert obj is not None
    assert obj["challenge_type"] == "cf_turnstile"
    assert obj["success"] is True and obj["token_len"] == 88


def test_unpack_rejects_plain_lines():
    assert unpack_result_line("element #5 scroll+circle+click") is None
    assert unpack_result_line("error: element #5 target gone") is None


def test_attempt_tail_roundtrip_and_reject():
    from src.capability.shield_solve import (
        ATTEMPT_MARKER,
        pack_attempt_tail,
        unpack_attempt_tail,
    )

    res = ShieldSolveResult(challenge_type="recaptcha", success=False,
                            token_len=0, elapsed_ms=15230,
                            error="no token within 15.0s")
    tail = pack_attempt_tail(res)
    assert ATTEMPT_MARKER in tail
    line = ("error: element #0 captcha/image challenge — escalating "
            f"(fallback toggle dispatched) {tail}")
    assert line.startswith("error:")
    obj = unpack_attempt_tail(line)
    assert obj is not None
    assert obj["success"] is False and obj["token_len"] == 0
    assert "recaptcha" in obj["challenge_type"]
    # Success lines carry the other marker; plain lines carry none.
    assert unpack_attempt_tail("element #0 challenge checkbox toggled") is None
    assert unpack_result_line(line) is None


# -- challenge_control wiring ------------------------------------------------

class _FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _ChallengePlatform:
    def __init__(self, page):
        self.page = page
        self._lock = _FakeLock()

    async def quiesce_idle_motion(self):
        return False

    def resume_idle_motion(self):
        pass


async def _covered_framed_probe(*a, **k):
    return ({"status": "covered", "verdict": "covered:iframe",
             "px": 10, "py": 20, "box": None, "live_kind": "iframe",
             "frame_embedded": True, "label": "robot"}, False)


def _checkbox_els():
    el = ElementRef(idx=2, kind="checkbox", label="I'm not a robot",
                    ref="f1e2")
    el.aria = "f1e2"
    return [ElementRef(idx=0, kind="link"),
            ElementRef(idx=1, kind="link"), el]


def test_challenge_control_shield_success_packed(monkeypatch):
    import src.actions as _actions
    import src.capability.shield_solve as _sh

    async def _det(platform, page_text=""):
        return ShieldDetection(detected=True, challenge_type="recaptcha",
                               confidence=0.9)

    async def _sol(platform, challenge_type, timeout_s=30.0):
        return ShieldSolveResult(challenge_type=challenge_type,
                                 success=True, token_len=64,
                                 elapsed_ms=900)

    monkeypatch.setattr(_actions, "verify_for_dispatch", _covered_framed_probe)
    monkeypatch.setattr(_sh, "detect_shield", _det)
    monkeypatch.setattr(_sh, "solve_shield", _sol)
    plat = _ChallengePlatform(_FakePage())
    res = _run(_actions.challenge_control(plat, _checkbox_els(), 2,
                                          expected_kind="checkbox"))
    assert "shield-bypass:" in res and not res.startswith("error")
    obj = unpack_result_line(res)
    assert obj["success"] is True and obj["token_len"] == 64


def test_challenge_control_shield_failure_falls_back_to_toggle(monkeypatch):
    import src.actions as _actions
    import src.capability.shield_solve as _sh

    async def _det(platform, page_text=""):
        return ShieldDetection(detected=True,
                               challenge_type="cf_turnstile",
                               confidence=0.95)

    async def _sol(platform, challenge_type, timeout_s=30.0):
        return ShieldSolveResult(challenge_type=challenge_type,
                                 success=False, elapsed_ms=300,
                                 error="no token in time")

    async def _toggle(platform, px, py, timeout=10.0, harvest=True,
                      native_aria=None, force=False):
        assert native_aria == "f1e2"
        assert force is True  # shield-checkbox path skips actionability waits
        return {"outcome": "settled", "info": "", "snapshot": ""}

    monkeypatch.setattr(_actions, "verify_for_dispatch", _covered_framed_probe)
    monkeypatch.setattr(_sh, "detect_shield", _det)
    monkeypatch.setattr(_sh, "solve_shield", _sol)
    monkeypatch.setattr(_actions, "dispatch_verified_click", _toggle)
    plat = _ChallengePlatform(_FakePage())
    plat.page.viewport_size = {"width": 1000, "height": 800}
    res = _run(_actions.challenge_control(plat, _checkbox_els(), 2,
                                          expected_kind="checkbox"))
    assert "challenge checkbox toggled" in res and "native locator" in res
