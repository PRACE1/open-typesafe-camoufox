"""tests for src/explain.py — offline capability Q&A + router + CLI wiring."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from src.explain import (
    TOPICS,
    answer,
    key_status,
    load_env_file,
    match,
    topic_names,
)


def test_topics_are_unique_and_indexed():
    names = topic_names()
    assert len(names) == len(set(names)), "duplicate topic keys"
    assert len(names) >= 15, "expected a broad topic set"
    for t in TOPICS:
        assert t.summary.strip(), f"empty summary on {t.key}"
        assert t.triggers, f"no triggers on {t.key}"
        for trig in t.triggers:
            assert trig.strip().lower() in [x.strip().lower() for x in t.triggers]


def test_router_finds_core_topics():
    assert any(t.key == "what-is-it" for t in match("what does this do?"))
    assert any(t.key == "bot-checks" for t in match("how are captcha bots solved?"))
    assert any(t.key == "frontier" for t in match("why does it keep revisiting the same links?"))
    assert any(t.key == "recent-updates" for t in match("what's new recently?"))
    assert any(t.key == "cost" for t in match("how much does a step cost?"))
    assert any(t.key == "usage" for t in match("how do I run a task?"))


def test_router_case_and_punctuation_insensitive():
    assert match("HOW ARE CAPTCHAS SOLVED?") == match("how are captchas solved?")


def test_answer_returns_text_on_match():
    out = answer("explain the loop")
    assert "phases" in out.lower()
    assert "the-loop" in out or "SEE" in out


def test_answer_no_match_returns_index():
    out = answer("zzzqqq flurble wibble")
    assert "I couldn't match" in out
    assert "what-is-it" in out  # index listing is present as a fallback


def test_answer_full_includes_details():
    brief = answer("what does this do?")
    full = answer("what does this do?", full=True)
    assert len(full) >= len(brief)


def test_load_env_file_missing_is_empty(tmp_path):
    assert load_env_file(str(tmp_path / "nope.env")) == {}


def test_load_env_file_parses(tmp_path):
    p = tmp_path / "e.env"
    p.write_text("# c\nGROQ_API_KEY=abc123\nJINA_API_KEY=\nBAD LINE\n", encoding="utf-8")
    got = load_env_file(str(p))
    assert got["GROQ_API_KEY"] == "abc123"
    assert got["JINA_API_KEY"] == "(empty)"
    assert "BAD" not in got


def test_key_status_masks_values(tmp_path, monkeypatch):
    p = tmp_path / ".env.local"
    p.write_text("GROQ_API_KEY=sk-super-secret-value\n", encoding="utf-8")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    out = key_status(path=str(p))
    assert "sk-super-secret-value" not in out, "secret value must not print"
    assert "GROQ_API_KEY" in out
    assert "masked" in out or "set" in out


def test_explain_module_imports_without_env(monkeypatch):
    for var in ("GROQ_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    import src.explain as ex  # noqa: F401
    assert ex.topic_names(), "topic index must build without any keys"


def test_cli_explain_offline(tmp_path):
    """otc.py with no --url answers offline without keys or browser."""
    env = {"PYTHONPATH": str(ROOT), "PATH": __import__("os").environ["PATH"]}
    import os
    env.update({k: v for k, v in os.environ.items()
                if k in ("PYTHONPATH", "PATH", "SYSTEMROOT")})
    for var in ("GROQ_API_KEY", "TYPESAFE_API_KEY"):
        env.pop(var, None)
    r = subprocess.run(
        [sys.executable, str(ROOT / "otc.py"), "--explain", "what's new?"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "Q: what's new?" in r.stdout
    assert "frontier" in r.stdout or "Recent update" in r.stdout


def test_cli_explain_lists_topics(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if k in ("PYTHONPATH", "PATH", "SYSTEMROOT")}
    for var in ("GROQ_API_KEY", "TYPESAFE_API_KEY"):
        env.pop(var, None)
    r = subprocess.run(
        [sys.executable, str(ROOT / "otc.py"), "--explain"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "Capability topics" in r.stdout
    assert "bot-checks" in r.stdout


def test_otc_explain_shim_offline(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if k in ("PYTHONPATH", "PATH", "SYSTEMROOT")}
    for var in ("GROQ_API_KEY", "TYPESAFE_API_KEY"):
        env.pop(var, None)
    r = subprocess.run(
        [sys.executable, str(ROOT / "otc_explain.py"), "how do I run it?"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "uv run otc.py" in r.stdout


def test_otc_explain_shim_lists(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if k in ("PYTHONPATH", "PATH", "SYSTEMROOT")}
    r = subprocess.run(
        [sys.executable, str(ROOT / "otc_explain.py"), "--list"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "recent-updates" in r.stdout
