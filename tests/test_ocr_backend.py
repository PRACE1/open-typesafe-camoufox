"""OCR backend tests — interface selection + service parsing (offline).

Local provider reuses the ddddocr sys.modules stub pattern from
test_captcha_ocr.py; the service provider stubs httpx at the client
level. No network, no weights, no Docker.
"""

import asyncio
import sys
import types

import pytest

from src.capability.backends import ocr as backends_ocr
from src.capability.backends.ocr import (
    LocalOcrBackend,
    ServiceOcrBackend,
    get_ocr_backend,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeOcr:
    def __init__(self, *a, **k):
        pass

    def classification(self, img, probability=False, **kwargs):
        assert isinstance(img, (bytes, bytearray))
        return {"charsets": ["", "A"], "probability": [[0.1, 0.9]]}

    def detection(self, img):
        return [[1, 2, 30, 40]]


def _install_ddddocr(monkeypatch):
    mod = types.ModuleType("ddddocr")
    mod.DdddOcr = _FakeOcr
    monkeypatch.setitem(sys.modules, "ddddocr", mod)
    import src.capability.captcha_ocr as co

    monkeypatch.setattr(co, "_ocr_instance", None)
    monkeypatch.setattr(co, "_det_instance", None)
    monkeypatch.setattr(co, "_slider_instance", None)


def test_selection_defaults_to_local(monkeypatch):
    monkeypatch.delenv("OCR_BACKEND", raising=False)
    assert isinstance(get_ocr_backend(), LocalOcrBackend)
    monkeypatch.setenv("OCR_BACKEND", "bogus")
    assert isinstance(get_ocr_backend(), LocalOcrBackend)


def test_selection_service(monkeypatch):
    monkeypatch.setenv("OCR_BACKEND", "service")
    monkeypatch.setenv("OCR_SERVICE_URL", "http://ocr:8000/")
    backend = get_ocr_backend()
    assert isinstance(backend, ServiceOcrBackend)
    assert backend.base_url == "http://ocr:8000"  # trailing slash stripped


def test_local_delegates_to_singletons(monkeypatch):
    _install_ddddocr(monkeypatch)
    backend = LocalOcrBackend()
    assert backend.available() is True
    assert backend.name == "local"
    res = backend.classify(b"png")
    assert res["probability"] == [[0.1, 0.9]]
    assert backend.detection(b"png") == [[1, 2, 30, 40]]


def test_local_unavailable_without_lib(monkeypatch):
    monkeypatch.setitem(sys.modules, "ddddocr", None)
    assert LocalOcrBackend().available() is False


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._body


class _FakeClient:
    seen = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, data=None):
        _FakeClient.seen.append((url, data))
        if url.endswith("/ocr"):
            return _FakeResponse({"code": 200, "message": "Success",
                                  "data": {"charsets": ["", "B"],
                                           "probability": [[0.2, 0.8]]}})
        return _FakeResponse({"code": 200, "message": "Success",
                              "data": [[5, 5, 50, 50]]})


def test_service_parses_api_response(monkeypatch):
    monkeypatch.setattr(backends_ocr.httpx, "Client", _FakeClient)
    _FakeClient.seen = []
    backend = ServiceOcrBackend("http://ocr:8000")
    assert backend.available() is True
    res = backend.classify(b"png")
    assert res["probability"] == [[0.2, 0.8]]
    url, form = _FakeClient.seen[0]
    assert url == "http://ocr:8000/ocr"
    assert form["probability"] == "true" and "image" in form
    assert backend.detection(b"png") == [[5, 5, 50, 50]]


def test_service_error_surfaces(monkeypatch):
    class _Bad(_FakeClient):
        def post(self, url, data=None):
            return _FakeResponse({"code": 500, "message": "boom"}, 200)

    monkeypatch.setattr(backends_ocr.httpx, "Client", _Bad)
    with pytest.raises(RuntimeError, match="boom"):
        ServiceOcrBackend("http://ocr:8000").classify(b"png")


def test_pipeline_flows_through_interface(monkeypatch):
    """solve_text_captcha consumes the backend (local stub here)."""
    import src.capability.captcha_ocr as co

    _install_ddddocr(monkeypatch)
    monkeypatch.delenv("OCR_BACKEND", raising=False)
    text, conf, cands, err = _run(co.solve_text_captcha(b"fake-png"))
    assert err is None and text == "A"
    assert conf == pytest.approx(0.9)
