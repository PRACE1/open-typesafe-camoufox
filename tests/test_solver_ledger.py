"""Solver ledger tests — stats derive from runs/*/steps.jsonl (offline).

The ledger has no sidecar file: runs ARE the memory. Tests build fake
run dirs (never the operator's real runs — conftest pins OTC_RUNS_DIR
at tmp), so derivation, ranking priors, and reordering are all
exercised against the true source of truth.
"""

import json
import os

from src.trajectory import solver_ledger as sl


def _write_steps(root, run, records):
    d = os.path.join(root, run)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "steps.jsonl"), "w",
              encoding="utf-8") as f:
        for i, rec in enumerate(records, 1):
            rec = dict(rec)
            rec.setdefault("n", i)
            f.write(json.dumps(rec) + "\n")


def test_stats_fold_blocks_and_toggle_text(tmp_path):
    root = str(tmp_path)
    _write_steps(root, "20260918-000001", [
        {"shield": {"success": False}, "result": "error toggling..."},
        {"shield": {"success": False}},
        {"grid": {"success": True, "rounds": 3}},
        {"captcha_ocr": {"available": True,
                         "data": {"tiles": [{"index": 0}]}}},
        {"result": "element #5 challenge checkbox toggled ok"},
        {"result": "idle wait"},
    ])
    table = sl.stats(root)
    assert table["capability:shield-bypass:checkbox"] == {"attempts": 2, "successes": 0,
                                          "rate": 0.0}
    assert table["capability:captchakraken:grid"]["rate"] == 1.0
    assert table["capability:ddddocr:ocr"]["rate"] == 1.0
    assert table["actions:toggle"] == {"attempts": 1, "successes": 1,
                                       "rate": 1.0}


def test_ocr_without_output_and_toggle_errors_count_as_failures(tmp_path):
    root = str(tmp_path)
    _write_steps(root, "20260918-000001", [
        {"captcha_ocr": {"available": True, "text": ""}},
        {"result": "error toggling challenge checkbox #5: boom"},
    ])
    table = sl.stats(root)
    assert table["capability:ddddocr:ocr"]["rate"] == 0.0
    assert table["actions:toggle"]["rate"] == 0.0


def test_missing_and_corrupt_runs_read_empty(tmp_path):
    assert sl.stats(str(tmp_path / "nope")) == {}
    d = os.path.join(str(tmp_path), "r1")
    os.makedirs(d)
    with open(os.path.join(d, "steps.jsonl"), "w",
              encoding="utf-8") as f:
        f.write("not json\n\n")
    assert sl.stats(str(tmp_path)) == {}


def test_priors_rank_cold_start_by_lived_experience(tmp_path):
    # Zero runs: operator priors rule — grid (CaptchaKraken, ~0.8)
    # first, shield and text-OCR sunk (0.0), toggle neutral (0.5).
    assert sl.score_for("capability:captchakraken:grid", {}) == 0.8
    assert sl.score_for("capability:shield-bypass:checkbox", {}) == 0.0
    assert sl.score_for("capability:ddddocr:ocr", {}) == 0.0
    assert sl.score_for("actions:toggle", {}) == 0.5
    assert sl.score_for("unknown-solver", {}) == 0.5
    names = ["capability:shield-bypass:checkbox", "actions:toggle", "capability:captchakraken:grid"]
    assert sl.rank_names(names, sl.stats(str(tmp_path))) == [
        "capability:captchakraken:grid", "actions:toggle", "capability:shield-bypass:checkbox"]


def test_ranking_needs_evidence_then_reorders(tmp_path):
    root = str(tmp_path)
    names = ["capability:shield-bypass:checkbox", "actions:toggle", "capability:captchakraken:grid"]
    _write_steps(root, "20260918-000001", [
        {"shield": {"success": False}},
        {"result": "error toggling challenge checkbox #5: x"},
    ])
    # One failure each: still priors, prior order preserved.
    assert sl.rank_names(names, sl.stats(root)) == [
        "capability:captchakraken:grid", "actions:toggle", "capability:shield-bypass:checkbox"]
    _write_steps(root, "20260918-000002", [
        {"shield": {"success": False}},
        {"shield": {"success": False}},
    ])
    ranked = sl.rank_names(names, sl.stats(root))
    assert ranked[-1] == "capability:shield-bypass:checkbox"


def test_success_floats_to_front(tmp_path):
    root = str(tmp_path)
    _write_steps(root, "20260918-000001", [
        {"grid": {"success": True}},
        {"grid": {"success": True}},
        {"grid": {"success": True}},
        {"shield": {"success": False}},
        {"shield": {"success": False}},
        {"shield": {"success": False}},
    ])
    ranked = sl.rank_names(["capability:shield-bypass:checkbox", "capability:captchakraken:grid"],
                           sl.stats(root))
    assert ranked == ["capability:captchakraken:grid", "capability:shield-bypass:checkbox"]
