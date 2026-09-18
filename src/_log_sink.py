"""Single append sink for all solver logs: console + persistent run.log.

Both logging_utils modules (run + capability) funnel through here so one
session log file (runs/<ts>/run.log) captures the full decision +
diagnostic feed: planner feed, element maps, nav recovery, tracker health.
"""

from __future__ import annotations

import re
import sys

_file = None


def set_file(path: str) -> None:
    global _file
    try:
        old = _file
        _file = open(path, "a", encoding="utf-8")
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
    except OSError:
        _file = None


def append(line: str) -> None:
    if _file is not None:
        try:
            _file.write(line + "\n")
            _file.flush()
        except OSError:
            pass


_ASCII_FALLBACKS = {
    "·": "-", "•": "-", "—": "-", "–": "-", "―": "-",
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>",
    "✓": "ok", "✔": "ok", "✗": "x", "✘": "x",
    "…": "...", "“": '"', "”": '"', "‘": "'", "’": "'",
    "⚠": "!", "⌀": "o", "✓️": "ok",
}


def console_safe(line: str) -> str:
    """Render a line for Windows consoles that mangle non-ASCII bytes.

    run.log (via ``append``) keeps the exact unicode; only the console
    print path transliterates box-drawing/prose symbols to ASCII so the
    live feed never prints mojibake (``�``/``?``) on cp1252 consoles.
    """
    for src, dst in _ASCII_FALLBACKS.items():
        if src in line:
            line = line.replace(src, dst)
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        line.encode(enc)
        return line
    except (UnicodeEncodeError, LookupError):
        return line.encode(enc, "replace").decode(enc, "replace")


_KV_MAX_CHARS = 160


def _kv_value(value: object) -> str:
    """Sanitize one kv field: flat, truncated, quoted when needed."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        try:
            import math as _math

            if _math.isfinite(float(value)):
                return str(value)
        except (ValueError, TypeError, OverflowError):
            pass
        return "-"
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v).replace(",", ";") for v in value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > _KV_MAX_CHARS:
        text = text[:_KV_MAX_CHARS - 3] + "..."
    if not text:
        return "-"
    if re.search(r"[\s=]", text):
        text = '"' + text.replace('"', "'") + '"'
    return text


def kv(tag: str, **fields: object) -> None:
    """One structured feed line: ``[tag] k=v k=v`` → console + run.log.

    Tag carries the full hierarchy (``capability:nav:response``,
    ``jev-solver:step:start``); values are flattened/truncated/quoted so
    every line stays greppable and single-line. Single print call, same
    interleave safety as emit_json. Never raises.
    """
    try:
        line = f"[{tag}] " + " ".join(
            f"{key}={_kv_value(val)}" for key, val in fields.items())
    except Exception:  # noqa: BLE001
        return
    append(line)
    try:
        print(console_safe(line), flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(line.encode(enc, "replace").decode(enc, "replace"), flush=True)


def format_json(obj: object) -> str:
    """Render a response record as formatted JSON.

    Same contract as the CrawlForAI (crawl4ai) library's
    ``extracted_content``: a JSON string the model infers structure
    from — indented, UTF-8, never Repr/prose. Non-serializable values
    fall back to ``str`` so emitting never raises on live objects.
    """
    import json as _json

    try:
        return _json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except (ValueError, TypeError):
        return _json.dumps({"unserializable": str(obj)[:500]},
                           ensure_ascii=False, indent=2)


def emit_json(obj: object) -> None:
    """Print one formatted-JSON response block, then append it to run.log.

    Single ``print`` call (no interleaved fragments on Windows consoles)
    plus ``console_safe`` rendering — this is the clean feed the
    browser-walk steps use for their structured responses.
    """
    text = format_json(obj)
    append(text)
    try:
        print(console_safe(text), flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(text.encode(enc, "replace").decode(enc, "replace"), flush=True)