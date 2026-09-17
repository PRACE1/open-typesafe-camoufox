"""Registry + validator tests — triple-gate enforcement (offline)."""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.capability.dynamic_registry import (
    clear_capabilities,
    get_capability,
    list_capabilities,
    register_capability,
)
from src.capability.validator import validate_capability

GOOD = """
import asyncio

LIMIT = 3

async def execute(platform, ref, ctx, dry_run=False):
    if dry_run:
        return ref + " dry-run ok"
    await asyncio.sleep(0)
    return ref + " acted"
"""


def _run(coro):
    return asyncio.run(coro)


def test_registry_round_trip():
    clear_capabilities()
    assert list_capabilities() == []

    async def _fn(platform, ref):
        return "ok"

    register_capability("heal_step3", _fn)
    assert get_capability("heal_step3") is _fn
    assert list_capabilities() == ["heal_step3"]
    assert get_capability("missing") is None
    clear_capabilities()


def test_validator_accepts_good_capability():
    res = _run(validate_capability(GOOD, "t", object(), "e2", {}))
    assert res.valid and res.error is None and res.func is not None


def test_validator_rejects_syntax_and_shape():
    bad_syntax = "async def execute(:\n  pass"
    assert _run(validate_capability(bad_syntax, "t", object(), "e1", {})).valid is False
    no_func = "X = 1\n"
    r = _run(validate_capability(no_func, "t", object(), "e1", {}))
    assert r.valid is False and "execute" in (r.error or "")
    two_funcs = GOOD + "\nasync def other(platform, ref):\n    return 'x'\n"
    assert _run(validate_capability(two_funcs, "t", object(), "e1", {})).valid is False
    top_expr = GOOD + "\nprint('side effect')\n"
    assert _run(validate_capability(top_expr, "t", object(), "e1", {})).valid is False


def test_validator_rejects_forbidden_modules_and_calls():
    for code in (
        "import os\nasync def execute(platform, ref):\n    return 'x'\n",
        "from subprocess import run\nasync def execute(platform, ref):\n    return 'x'\n",
        "import mydriver\nasync def execute(platform, ref):\n    return 'x'\n",
        "async def execute(platform, ref):\n    return eval('1')\n",
        "async def execute(platform, ref):\n    open('/tmp/x').read()\n    return 'x'\n",
    ):
        r = _run(validate_capability(code, "t", object(), "e1", {}))
        assert r.valid is False, code


def test_validator_rejects_bad_signature():
    sync_fn = "def execute(platform, ref):\n    return 'x'\n"
    assert _run(validate_capability(sync_fn, "t", object(), "e1", {})).valid is False
    wrong_names = "async def execute(page, target):\n    return 'x'\n"
    r = _run(validate_capability(wrong_names, "t", object(), "e1", {}))
    assert r.valid is False and "platform" in (r.error or "")


def test_validator_dry_run_must_return_str_without_raising():
    raising = ("async def execute(platform, ref, ctx, dry_run=False):\n"
               "    raise RuntimeError('boom')\n")
    r = _run(validate_capability(raising, "t", object(), "e1", {}))
    assert r.valid is False and "dry-run raised" in (r.error or "")
    non_str = ("async def execute(platform, ref, ctx, dry_run=False):\n"
               "    return 42\n")
    r = _run(validate_capability(non_str, "t", object(), "e1", {}))
    assert r.valid is False and "str" in (r.error or "")


def test_validator_dry_run_timeout():
    slow = ("import asyncio\n"
            "async def execute(platform, ref, ctx, dry_run=False):\n"
            "    await asyncio.sleep(30)\n"
            "    return 'x'\n")
    r = _run(validate_capability(slow, "t", object(), "e1", {}))
    assert r.valid is False and "exceeded" in (r.error or "")


def test_validator_rejects_missing_dry_run_kwarg_gracefully():
    """A capability without dry_run in its signature fails closed: the
    dry-run call raises TypeError, which the gate reports (never executes
    the live path during validation)."""
    no_kwarg = ("async def execute(platform, ref, ctx):\n"
                "    return ref + ' acted'\n")
    r = _run(validate_capability(no_kwarg, "t", object(), "e1", {}))
    assert r.valid is False
