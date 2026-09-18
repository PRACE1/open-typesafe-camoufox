"""backends package — provider interfaces behind the capability tools.

Callers (``captcha_ocr``, grid workflow, challenge policy) program to
``OcrBackend`` / ``PaidSolver`` and never import a concrete engine.
Selection is env-driven at call time (``OCR_BACKEND``, ``OCR_SERVICE_URL``);
tests stub the interface instead of ``sys.modules`` engines.

Concrete providers today: local ddddocr singletons, ddddocr-fastapi
service (``sml2h3/ddddocr-fastapi``: ``APIResponse{code,message,data}``
over form POSTs), 2captcha paid submits. Future: YOLO/CLIP tile
classifiers slot in as new providers with no call-site changes.
"""

from __future__ import annotations

from .ocr import OcrBackend, get_ocr_backend
from .solver import PaidSolver, get_paid_solver

__all__ = ["OcrBackend", "PaidSolver", "get_ocr_backend", "get_paid_solver"]
