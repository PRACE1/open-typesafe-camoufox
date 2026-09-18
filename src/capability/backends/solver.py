"""solver.py — paid-token solver interface (2captcha today, vision later).

``PaidSolver.submit`` returns ``(token, logs)``: the token string (or
"" on any failure, reason appended to logs) so the caller can inject
it immediately and discard it. Tokens never enter records — lengths
only, same hygiene as every other solver in this repo.

Selection is capability-driven, not env-driven: ``get_paid_solver``
returns the first configured provider (2captcha when key + proxy are
set), or None when no paid path exists and the caller must escalate
honestly instead of spending money it doesn't have.
"""

from __future__ import annotations

from typing import Protocol


class PaidSolver(Protocol):
    """One paid submit → token for immediate injection."""

    name: str

    def available(self) -> bool:
        """True when key + egress are configured (no network use)."""
        ...

    async def submit(self, sitekey: str, url: str,
                     timeout_s: float) -> tuple[str, list[str]]:
        """Token string ("" on failure, reason in logs). Never raises."""
        ...


class TwoCaptchaPaidSolver:
    """2captcha worker pool via ``twocaptcha_client`` (no new code path)."""

    name = "twocaptcha"

    def available(self) -> bool:
        # Lazy import: twocaptcha_client is a sibling leaf; keep this
        # module importable without optional deps installed.
        from ..twocaptcha_client import is_available

        return is_available()

    async def submit(self, sitekey: str, url: str,
                     timeout_s: float) -> tuple[str, list[str]]:
        from ..twocaptcha_client import _env, _submit_once, proxy_for_api

        logs = [f"paid submit via {self.name}"]
        key, _ = _env()
        proxy = proxy_for_api()
        if not key or proxy is None:
            logs.append("refused: key or proxy missing")
            return "", logs
        token = await _submit_once(key, sitekey, url, proxy, timeout_s,
                                   logs)
        return token, logs


def get_paid_solver() -> PaidSolver | None:
    """First configured paid provider, or None (escalate, don't spend)."""
    solver = TwoCaptchaPaidSolver()
    return solver if solver.available() else None
