"""validator.py — triple-gate validation for synthesized capabilities.

A writer-composed capability must pass all three gates before it touches
the live page:

1. AST audit (no execution): syntax must parse; top level holds only
   imports of asyncio, constant assigns, and one `async def execute`;
   forbidden modules (os/sys/subprocess/socket/requests/shutil) and calls
   (eval/exec/__import__/compile/open) are rejected on sight.
2. Signature contract: `execute(platform, ref, ...)` — platform first,
   ref second, both positional-or-keyword; must be a coroutine function.
3. Shadow dry-run: `execute(platform, ref, ctx, dry_run=True)` must return
   a string within 3s without raising. The skeleton's dry-run branch is
   probe-only by construction; the gate proves crash-freedom and
   resolvability, not side-effect-freedom (documented limit).

Gate failure returns valid=False with the reason; the runner accounts it
as heal_failed and never registers the code.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

FORBIDDEN_CALLS = {"eval", "exec", "__import__", "compile", "open"}
FORBIDDEN_MODULES = {"os", "sys", "subprocess", "socket", "requests",
                     "shutil", "pathlib", "pty", "signal"}

EXPECTED_FUNC = "execute"
DRY_RUN_TIMEOUT = 3.0


@dataclass
class ValidationResult:
    """Outcome of compile_and_validate_capability."""

    valid: bool
    error: str | None = None
    func: Callable[..., Coroutine[Any, Any, Any]] | None = None


def _audit(tree: ast.Module) -> str | None:
    """Return a rejection reason, or None when the AST is clean."""
    seen_execute = False
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    return f"forbidden import: {alias.name}"
                if alias.name.split(".")[0] != "asyncio":
                    return f"only asyncio may be imported, got: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_MODULES:
                return f"forbidden from-import: {node.module}"
            if mod != "asyncio":
                return f"only asyncio may be imported, got: {node.module}"
        elif isinstance(node, ast.AsyncFunctionDef):
            if node.name != EXPECTED_FUNC:
                return f"only async def {EXPECTED_FUNC} allowed, got: {node.name}"
            if seen_execute:
                return f"duplicate async def {EXPECTED_FUNC}"
            seen_execute = True
        elif isinstance(node, ast.Assign):
            continue  # module constants are inert
        else:
            return f"top-level {type(node).__name__} not allowed"
    if not seen_execute:
        return f"missing async def {EXPECTED_FUNC}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            return f"forbidden call: {node.id}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    return f"forbidden import: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            if mod in FORBIDDEN_MODULES:
                return f"forbidden from-import: {node.module}"
    return None


def _signature_ok(func: Any) -> str | None:
    """Return a rejection reason, or None when the contract holds."""
    if not inspect.iscoroutinefunction(func):
        return f"{EXPECTED_FUNC} must be async"
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return "signature unreadable"
    kinds = {inspect.Parameter.POSITIONAL_ONLY,
             inspect.Parameter.POSITIONAL_OR_KEYWORD}
    positional = [p.name for p in params if p.kind in kinds]
    if len(positional) < 2 or positional[0] != "platform" or positional[1] != "ref":
        return (f"signature must start (platform, ref, ...); "
                f"found: {[p.name for p in params]}")
    return None


async def validate_capability(code: str, name: str, platform: Any,
                              ref: str, ctx: dict) -> ValidationResult:
    """Compile, audit, contract-check, and dry-run one capability.

    Returns the executable on full pass; valid=False with a reason
    otherwise. The dry-run executes `execute(platform, ref, ctx,
    dry_run=True)` under a 3s timeout.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return ValidationResult(valid=False, error=f"syntax: {exc}")
    reason = _audit(tree)
    if reason is not None:
        return ValidationResult(valid=False, error=reason)
    scope: dict[str, Any] = {"__name__": f"<healed:{name}>"}
    try:
        exec(compile(tree, filename=f"<healed:{name}>", mode="exec"), scope)
    except Exception as exc:  # noqa: BLE001
        return ValidationResult(valid=False, error=f"load: {exc}")
    func = scope.get(EXPECTED_FUNC)
    reason = _signature_ok(func)
    if reason is not None:
        return ValidationResult(valid=False, error=reason)
    try:
        out = await asyncio.wait_for(
            func(platform, ref, ctx, dry_run=True),
            timeout=DRY_RUN_TIMEOUT)
    except asyncio.TimeoutError:
        return ValidationResult(valid=False,
                                error=f"dry-run exceeded {DRY_RUN_TIMEOUT}s")
    except Exception as exc:  # noqa: BLE001
        return ValidationResult(valid=False, error=f"dry-run raised: {exc}")
    if not isinstance(out, str):
        return ValidationResult(valid=False,
                                error="dry-run must return a history-line str")
    return ValidationResult(valid=True, func=func)
