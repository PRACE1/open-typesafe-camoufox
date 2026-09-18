"""Shared fixtures: isolate tests from the operator's workspace.

Challenge policy derives solver effectiveness from runs/*/steps.jsonl.
Pointing OTC_RUNS_DIR at an empty tmp dir keeps unit tests
deterministic regardless of local run history (and prevents test
writes from ever touching real runs).
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OTC_RUNS_DIR", str(tmp_path / "runs"))
