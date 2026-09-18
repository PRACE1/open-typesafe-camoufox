"""ocr.py — OCR provider interface (local ddddocr | ddddocr-fastapi service).

Upstream service: https://github.com/sml2h3/ddddocr-fastapi (MIT).
Endpoints take form fields and return ``APIResponse{code, message,
data}`` where ``data`` is the RAW ddddocr return — a classification
dict/str for ``/ocr`` (``probability=true`` yields the ``{charsets,
probability}`` distribution our top-k ranking consumes) and a bbox
list for ``/detection``. So the service is a transport swap, not a
format swap: downstream ranking/detection-box code is untouched.

Selection: ``OCR_BACKEND`` = ``local`` (default) | ``service``.
``OCR_SERVICE_URL`` defaults to the service's own default port
(``http://127.0.0.1:8000``). Unknown values fall back to local.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Protocol

import httpx

SERVICE_DEFAULT_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_S = 30.0


class OcrBackend(Protocol):
    """Raw ddddocr-shaped OCR engine behind one interface."""

    name: str

    def available(self) -> bool:
        """True when this backend can serve right now (no model load)."""
        ...

    def classify(self, png: bytes, probability: bool = True) -> Any:
        """Raw classification return (dict distribution or plain string)."""
        ...

    def detection(self, png: bytes) -> list:
        """Raw detection return (list of bbox rows)."""
        ...


class LocalOcrBackend:
    """In-process ddddocr singletons (per-mode lazy instances)."""

    name = "local"

    def available(self) -> bool:
        # Lazy import: this module is imported BY captcha_ocr, so a
        # top-level import back would cycle.
        from ..captcha_ocr import is_available

        return is_available()

    def _instance(self, mode: str) -> Any:
        from ..captcha_ocr import _get_instance

        return _get_instance(mode)

    def classify(self, png: bytes, probability: bool = True) -> Any:
        return self._instance("ocr").classification(png, probability=True)

    def detection(self, png: bytes) -> list:
        return list(self._instance("det").detection(png) or [])


class ServiceOcrBackend:
    """ddddocr-fastapi over HTTP (same models, remote process)."""

    name = "service"

    def __init__(self, base_url: str = "") -> None:
        self.base_url = (base_url or os.environ.get(
            "OCR_SERVICE_URL", "") or SERVICE_DEFAULT_URL).rstrip("/")

    def available(self) -> bool:
        # No model load to probe cheaply; configured URL == available.
        # Call failures surface per-call as structured errors, never raise.
        return bool(self.base_url)

    def _post(self, path: str, form: dict[str, str]) -> Any:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S) as client:
            res = client.post(self.base_url + path, data=form)
            res.raise_for_status()
            body = res.json()
        if not isinstance(body, dict) or body.get("code") != 200:
            raise RuntimeError(
                str((body or {}).get("message", "ocr service error"))[:200])
        return body.get("data")

    def classify(self, png: bytes, probability: bool = True) -> Any:
        return self._post("/ocr", {
            "image": base64.b64encode(png).decode("ascii"),
            "probability": "true" if probability else "false",
        })

    def detection(self, png: bytes) -> list:
        data = self._post("/detection", {
            "image": base64.b64encode(png).decode("ascii"),
        })
        return list(data or [])


def get_ocr_backend() -> OcrBackend:
    """Select the OCR backend from ``OCR_BACKEND`` (call time)."""
    which = (os.environ.get("OCR_BACKEND", "") or "local").strip().lower()
    if which == "service":
        return ServiceOcrBackend()
    return LocalOcrBackend()
