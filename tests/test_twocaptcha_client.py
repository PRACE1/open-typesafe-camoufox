"""2captcha client tests — proxy assembly, secret masking, solve flow (offline).

The ``twocaptcha`` lib is stubbed in sys.modules (same pattern as the
ddddocr stub in test_captcha_ocr.py) so no key, proxy, or network is
ever needed. Secrets must never appear in records, logs, or lines.
"""

import asyncio
import sys
import types

import pytest

from src.capability import twocaptcha_client as tc


PROXY_ENV = {
    "APIKEY_2CAPTCHA": "test-key-123",
    "TWOCAPTCHA_PROXY_TYPE": "HTTPS",
    "TWOCAPTCHA_PROXY_HOST": "ap.proxy.2captcha.com",
    "TWOCAPTCHA_PROXY_PORT": "2334",
    "TWOCAPTCHA_PROXY_USER": "user-zone-custom",
    "TWOCAPTCHA_PROXY_PASS": "pass-secret-xyz",
}


def _set_proxy_env(monkeypatch, **overrides):
    env = dict(PROXY_ENV)
    env.update(overrides)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _clear_env(monkeypatch):
    for k in PROXY_ENV:
        monkeypatch.delenv(k, raising=False)


def _run(coro):
    return asyncio.run(coro)


class _FakeSolver:
    def __init__(self, *a, **k):
        self.seen = {}
        _FakeSolver.last = self

    def recaptcha(self, sitekey, url, **kwargs):
        self.seen = {"sitekey": sitekey, "url": url, **kwargs}
        return {"code": "TOKEN-ABC-123"}


def _install_stub(monkeypatch, solver=None):
    mod = types.ModuleType("twocaptcha")
    mod.TwoCaptcha = solver or _FakeSolver
    monkeypatch.setitem(sys.modules, "twocaptcha", mod)
    return mod


# -- env / proxy assembly -------------------------------------------------

def test_proxy_for_api_shape(monkeypatch):
    _set_proxy_env(monkeypatch)
    assert tc.proxy_for_api() == {
        "type": "HTTPS",
        "uri": "user-zone-custom:pass-secret-xyz@ap.proxy.2captcha.com:2334",
    }


def test_proxy_incomplete_refuses(monkeypatch):
    _set_proxy_env(monkeypatch, TWOCAPTCHA_PROXY_PASS="")
    assert tc.proxy_configured() is False
    assert tc.proxy_for_api() is None
    assert tc.browser_proxy() is None


def test_masked_proxy_hides_secrets(monkeypatch):
    _set_proxy_env(monkeypatch)
    masked = tc.masked_proxy()
    assert "pass-secret-xyz" not in masked
    assert "user-zone-custom" not in masked
    assert "ap.proxy.2captcha.com" in masked and "2334" in masked


def test_browser_proxy_server_format(monkeypatch):
    _set_proxy_env(monkeypatch)
    assert tc.browser_proxy() == {
        "server": "http://ap.proxy.2captcha.com:2334",
        "username": "user-zone-custom",
        "password": "pass-secret-xyz",
    }
    _set_proxy_env(monkeypatch, TWOCAPTCHA_PROXY_TYPE="socks5",
                   TWOCAPTCHA_PROXY_PORT="2333")
    assert tc.browser_proxy()["server"] == \
        "socks5://ap.proxy.2captcha.com:2333"


def test_is_available_needs_key_and_lib(monkeypatch):
    _clear_env(monkeypatch)
    _install_stub(monkeypatch)
    assert tc.is_available() is False  # no key
    _set_proxy_env(monkeypatch)
    assert tc.is_available() is True
    monkeypatch.setitem(sys.modules, "twocaptcha", None)  # import blows up
    assert tc.is_available() is False  # no lib


# -- solve flow ------------------------------------------------------------

def test_solve_refuses_without_key(monkeypatch):
    _clear_env(monkeypatch)
    res = _run(tc.solve_recaptcha("sitekey", "https://x.test/"))
    assert res.success is False and "APIKEY_2CAPTCHA" in (res.error or "")


def test_solve_refuses_without_proxy(monkeypatch):
    _set_proxy_env(monkeypatch, TWOCAPTCHA_PROXY_HOST="")
    _install_stub(monkeypatch)
    res = _run(tc.solve_recaptcha("sitekey", "https://x.test/"))
    assert res.success is False and "proxy" in (res.error or "").lower()


def test_solve_success_records_length_only(monkeypatch):
    _set_proxy_env(monkeypatch)
    _install_stub(monkeypatch)
    res = _run(tc.solve_recaptcha("sitekey", "https://x.test/"))
    assert res.success is True and res.token_len == len("TOKEN-ABC-123")
    rec = res.to_record()
    assert "TOKEN-ABC-123" not in str(rec)
    assert "pass-secret-xyz" not in str(rec)
    assert "test-key-123" not in str(rec)
    assert rec["logs"]  # trail lives inside the response


def test_solve_forwards_proxy_to_api(monkeypatch):
    _set_proxy_env(monkeypatch)
    _install_stub(monkeypatch)
    _run(tc.solve_recaptcha("sitekey", "https://x.test/"))
    assert _FakeSolver.last.seen["proxy"]["uri"].startswith(
        "user-zone-custom:")


# -- sitekey extraction -----------------------------------------------------

def test_sitekey_from_anchor_src():
    assert tc.sitekey_from_anchor_src(
        "https://www.google.com/recaptcha/api2/anchor?k=ABC123xyz&co=abc"
    ) == "ABC123xyz"
    assert tc.sitekey_from_anchor_src("https://example.test/") == ""


class _AttrLocator:
    def __init__(self, count=0, attr=""):
        self._count = count
        self._attr = attr

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def get_attribute(self, name):
        return self._attr


class _KeyPage:
    def __init__(self, div_key="", frame_src=""):
        self._div_key = div_key
        self._frame_src = frame_src

    def locator(self, sel):
        if "data-sitekey" in sel:
            return _AttrLocator(count=1 if self._div_key else 0,
                                attr=self._div_key)
        return _AttrLocator(count=1 if self._frame_src else 0,
                            attr=self._frame_src)


def test_extract_sitekey_prefers_div():
    page = _KeyPage(div_key="DIVKEY",
                    frame_src="https://x.test/anchor?k=FRAMEKEY")
    assert _run(tc.extract_sitekey(page)) == "DIVKEY"


def test_extract_sitekey_falls_back_to_anchor():
    page = _KeyPage(frame_src="https://x.test/anchor?k=FRAMEKEY&co=z")
    assert _run(tc.extract_sitekey(page)) == "FRAMEKEY"


def test_extract_sitekey_empty_when_absent():
    assert _run(tc.extract_sitekey(_KeyPage())) == ""


# -- pack/unpack --------------------------------------------------------------

def test_pack_unpack_roundtrip():
    res = tc.TwoCaptchaResult(available=True, success=True, token_len=64,
                              elapsed_ms=900, logs=["submitted"])
    line = tc.pack_result_line(4, res)
    assert tc.RESULT_MARKER in line and "solved" in line
    obj = tc.unpack_result_line(line)
    assert obj is not None and obj["success"] is True
    assert obj["token_len"] == 64 and obj["logs"] == ["submitted"]


def test_unpack_rejects_plain_lines():
    assert tc.unpack_result_line("element #5 scroll+circle+click") is None
    assert tc.unpack_result_line("error: element #5 target gone") is None


# -- submit+inject --------------------------------------------------------------

class _InjectPage:
    def __init__(self):
        self.seen_token = None

    async def evaluate(self, js, arg=None):
        assert "g-recaptcha-response" in js
        self.seen_token = arg
        return "callback"


def test_submit_injects_and_discards_token(monkeypatch):
    _set_proxy_env(monkeypatch)
    _install_stub(monkeypatch)
    page = _InjectPage()
    res = _run(tc.submit_recaptcha_token(page, "sitekey", "https://x.test/"))
    assert res.success is True
    assert page.seen_token == "TOKEN-ABC-123"  # value reached the page
    assert "TOKEN-ABC-123" not in str(res.to_record())
    assert res.data.get("injected") == "callback"


# -- challenge_control last-resort wiring ----------------------------------------

def test_challenge_paid_fallback_fires_on_toggle_error(monkeypatch):
    import src.actions as _actions

    _set_proxy_env(monkeypatch)
    _install_stub(monkeypatch)

    async def _boom_toggle(platform, px, py, timeout=10.0, harvest=True,
                           native_aria=None, force=False):
        return {"outcome": "error", "info": "dispatch failed: boom",
                "snapshot": ""}

    async def _covered_framed_probe(platform, elements, idx, expected=None):
        return ({"status": "covered", "verdict": "covered:iframe",
                 "px": 10, "py": 20, "box": None, "live_kind": "iframe",
                 "frame_embedded": True, "label": "robot"}, False)

    async def _fake_sitekey(page):
        return "SITEKEY-1"

    async def _fake_submit(page, sitekey, url, timeout_s=120.0):
        assert sitekey == "SITEKEY-1"
        return tc.TwoCaptchaResult(available=True, success=True,
                                   token_len=32, elapsed_ms=10,
                                   logs=["submitted"])

    monkeypatch.setattr(_actions, "verify_for_dispatch", _covered_framed_probe)
    monkeypatch.setattr(_actions, "dispatch_verified_click", _boom_toggle)
    monkeypatch.setattr(tc, "extract_sitekey", _fake_sitekey)
    monkeypatch.setattr(tc, "submit_recaptcha_token", _fake_submit)

    from src.deps import ElementRef

    el = ElementRef(idx=2, kind="checkbox", label="I'm not a robot",
                    ref="f1e2")
    el.aria = "f1e2"
    plat = _KeyPlatform()
    res = _run(_actions.challenge_control(plat, [el], 2,
                                          expected_kind="checkbox"))
    assert "2captcha-python:" in res and not res.startswith("error")
    obj = tc.unpack_result_line(res)
    assert obj is not None and obj["success"] is True


def test_challenge_no_key_keeps_old_error(monkeypatch):
    import src.actions as _actions

    _clear_env(monkeypatch)

    async def _boom_toggle(platform, px, py, timeout=10.0, harvest=True,
                           native_aria=None, force=False):
        return {"outcome": "error", "info": "dispatch failed: boom",
                "snapshot": ""}

    async def _covered_framed_probe(platform, elements, idx, expected=None):
        return ({"status": "covered", "verdict": "covered:iframe",
                 "px": 10, "py": 20, "box": None, "live_kind": "iframe",
                 "frame_embedded": True, "label": "robot"}, False)

    monkeypatch.setattr(_actions, "verify_for_dispatch", _covered_framed_probe)
    monkeypatch.setattr(_actions, "dispatch_verified_click", _boom_toggle)

    from src.deps import ElementRef

    el = ElementRef(idx=2, kind="checkbox", label="I'm not a robot",
                    ref="f1e2")
    el.aria = "f1e2"
    plat = _KeyPlatform()
    res = _run(_actions.challenge_control(plat, [el], 2,
                                          expected_kind="checkbox"))
    assert res.startswith("error") and "boom" in res


class _KeyPlatform:
    def __init__(self):
        from asyncio import Lock

        self.page = _KeyPage()
        self._lock = Lock()

    async def quiesce_idle_motion(self):
        return False

    def resume_idle_motion(self):
        pass
