"""dynamic_registry.py — runtime registration of healed capabilities.

In-memory only (dies with the session): triage-approved synthesized
capabilities register here under ephemeral names (heal_step<N>) and execute
on the very next beat. Nothing enters the registry without passing
validator.compile_and_validate_capability first — the runner enforces that
order, this module only holds the table.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

CapabilityFunc = Callable[..., Coroutine[Any, Any, Any]]

_REGISTRY: dict[str, CapabilityFunc] = {}


def register_capability(name: str, func: CapabilityFunc) -> None:
    """Register a validated capability under an ephemeral name."""
    _REGISTRY[name] = func


def get_capability(name: str) -> CapabilityFunc | None:
    """Fetch a registered capability, or None when unknown."""
    return _REGISTRY.get(name)


def list_capabilities() -> list[str]:
    """Names currently in the registry (usually heal_step<N> entries)."""
    return list(_REGISTRY.keys())


def clear_capabilities() -> None:
    """Empty the registry (tests; sessions die with their own table)."""
    _REGISTRY.clear()
