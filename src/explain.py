"""explain.py — plain-English capability Q&A for outsiders.

Outsiders who can't be bothered to read the README can run:

    uv run otc_explain.py "what does this do?"
    uv run otc.py --explain "how do bots get solved?"

Each topic below is a short, no-jargon answer (a person who has never
seen this repo should understand it) plus plain-language triggers used
for keyword routing. Routing is pure string matching — no API keys,
no model calls — so it always works offline and answers honestly fall
back to the topic index.

Topics intentionally include "what we updated recently" entries so
the Q&A surfaces recent work, not just the old shape.
"""

from __future__ import annotations

import os
import re

_TOPIC_INDEX: dict[str, str] = {}


class Topic:
    """One capability, explained for someone who has never read the README."""

    def __init__(self, key: str, summary: str, detail: str | None = "",
                 triggers: tuple[str, ...] | None = None) -> None:
        self.key = key
        # Callers that omit detail pass the trigger tuple in the 3rd slot;
        # normalize both shapes here so the rest of the module sees
        # canonical (str detail, tuple triggers).
        if isinstance(detail, tuple) and triggers is None:
            detail, triggers = "", detail
        self.summary = summary
        self.detail = detail or ""
        self.triggers = tuple(triggers or ())

    def __str__(self) -> str:
        out = f"- {self.summary}"
        if self.detail:
            out += f"\n  {self.detail}"
        return out


TOPICS: list[Topic] = [
    Topic(
        "gtm-context",
        "Why this exists: a go-to-market engineer wanted the browser loop "
        "itself to pick the cheapest reliable decision model — not a big "
        "LLM per step, not a hardcoded model. Jev (~$0.0002, ~200 ms) "
        "decides one action per step; a small writer only writes words. "
        "The state machine (see → decide → gate → act → verify) makes the "
        "whole system replaceable without the loop semantics changing.",
        "Read docs/WHY_THIS_EXISTS.md for the full guide: the belief, "
        "the journey, the machine, and why each part is there. The "
        "GTM application section covers prospect research, VSL "
        "walkthroughs, and scaling across lead lists.",
        ("gtm", "go to market", "agency", "why this", "why does this "
         "exist", "matthew", "journey", "cheapest model", "lowest cost",
         "state machine", "scale"),
    ),
    Topic(
        "what-is-it",
        "A program that drives a real, visible browser (Camoufox) toward a goal "
        "you type in plain English. You give it a URL and a task; it clicks, "
        "types, and reads pages the way a person would — each step costs "
        "about a tenth of a cent and leaves a full audit trail you can "
        "replay offline later.",
        "The headline property: it never sends a screenshot to a big "
        "expensive model. It reads the page deterministically and asks a "
        "tiny fast classifier which single action to take next.",
        ("what is this", "what does this do", "explain", "overview", "intro",
         "how does this work", "tell me about"),
    ),
    Topic(
        "the-loop",
        "Every step walks the same five phases: SEE (read the page), "
        "DECIDE (pick one action), GATE (confidence check), ACT (do it "
        "with a human-looking cursor), VERIFY (did it work). The loop "
        "keeps going until the task is observably complete, the budget "
        "runs out, or a stop rule honestly ends it.",
        "Stalls are honest: if a page looks identical to the last step "
        "twice in a row, the run stops instead of burning steps.",
        ("loop", "step", "phases", "see decide act", "how does it work",
         "phase", "machine"),
    ),
    Topic(
        "jev-classifier",
        "The brain is a small decision model (Jev / 'System One') that "
        "answers multiple-choice questions: which of 12 action verbs "
        "comes next, which element to click, which URL to visit, plus "
        "five yes/no flags and a progress score. Each answer comes with "
        "a confidence number that the gates use to decide whether to act "
        "or idle.",
        "One request per step, a few hundred milliseconds, fraction of a "
        "cent. The model id that answered is recorded per run.",
        ("jev", "classifier", "model", "decide", "decision", "choose",
         "how does it decide", "what decides", "confidence"),
    ),
    Topic(
        "decision-questions",
        "Each step, Jev answers: kind (one of 12 verbs — wait, "
        "click, type, press enter, refresh, back, close tabs, goto, "
        "challenge, done, ...), item (which element ref eN), site "
        "(which URL from the catalog), five flags (page ready?, needs "
        "text?, task done?, element fits?, approve this proposal?), and "
        "a progress score.",
        "Options are mutually exclusive and carry what/what-not-for "
        "boundaries so the model reads doubt instead of guessing.",
        ("questions", "choice", "noul", "score", "kind", "verb",
         "what are the kinds", "what are the questions"),
    ),
    Topic(
        "writer-model",
        "A small writing model is only called when a text field genuinely "
        "needs fresh words — never for decisions, clicks, or waits. It "
        "composes the text and nothing else; code revalidates URLs and "
        "refuses to fill credentials without task placeholders.",
        ("writer", "free text", "type text", "words", "language model",
         "groq", "generates"),
    ),
    Topic(
        "bot-checks",
        "When a bot-check or CAPTCHA appears, a ranked chain of solvers "
        "attempts it: native checkbox/token technique first, offline OCR "
        "for image grids, a hosted vision grid solver, and a paid "
        "fallback — in that order, re-ranked by what has actually "
        "worked in past runs (the solver ledger).",
        "Rules: an open grid is never re-clicked (that closes the "
        "popup); paid solvers only plan when their key + proxy are "
        "configured; unconfigured backends refuse loudly and cost "
        "nothing.",
        ("captcha", "bot check", "bot-check", "recaptcha", "turnstile",
         "solver", "shield", "ocr", "ddoc", "kraken", "2captcha",
         "verification", "challenge"),
    ),
    Topic(
        "humanized-cursor",
        "Clicks and typing use human-looking cursor motion (short "
        "bezier paths, a highlight ring on hover) so the browser "
        "behaves like a person moving a mouse, not a pixel-jumping "
        "script.",
        "Recent update: browser-level smoothing (Camoufox humanize) is "
        "OFF by default after it was measured to degrade per-dispatch "
        "reliability; our own 5-hop highlight at level 0.3 keeps the "
        "human feel at ~0.35s per move.",
        ("cursor", "mouse", "human", "humanize", "smooth", "bezier",
         "movement"),
    ),
    Topic(
        "run-folder",
        "Every run writes a folder of evidence: raw + annotated "
        "screenshots per step, the exact model payload, all probability "
        "answers, a machine-readable step log, cursor trail, page memory "
        "chunks, and solver audit records. You can grep one tag to "
        "isolate one subsystem.",
        "That trail is also the offline replay: uv run otc.py --replay "
        "runs/<folder> --replay-step N shows what the model saw and "
        "decided without a browser.",
        ("run folder", "runfolder", "replay", "logs", "audit", "trace",
         "steps.jsonl", "what gets saved", "offline"),
    ),
    Topic(
        "page-memory",
        "Each settled page is fetched as structured JSON (via the Jina "
        "Reader, with a DOM-extraction fallback), chunked, scored "
        "against the task, and stored. On later steps the best chunks "
        "are recalled into the state packet, so the model reasons about "
        "everything it has already read — not just the last step.",
        ("memory", "remember", "recall", "jina", "reader", "long "
         "horizon", "notes"),
    ),
    Topic(
        "frontier",
        "Recent update: a persistent to-do ledger (the Frontier) stops "
        "long runs from re-opening the same links forever. It keys "
        "targets by host + visible label instead of URL (search engines "
        "wrap links in opaque redirects), learns which label goes to "
        "which real URL as it visits, and marks visited targets loudly "
        "in every element list so the classifier routes around them.",
        ("frontier", "crawl", "revisit", "revisiting", "re-visit", "re-open",
         "visited", "to-do", "todo", "queue", "ledger", "same links",
         "same link"),
    ),
    Topic(
        "mission",
        "Recent update: --mission runs long collection tasks "
        "resume-until-done. When a session stops (a transient stall, "
        "budget hit), the next one relaunches with every URL already "
        "covered seeded in, sharing the total step/wall-clock budget "
        "(max 10 sessions). A mission only ends when it is done, "
        "exhausted, or produces an empty session.",
        ("mission", "long run", "resume", "collection", "sessions",
         "restart"),
    ),
    Topic(
        "recovery",
        "Recent update: when a click lands on stale or covered "
        "territory, the step detours act → heal → act. Jev triages the "
        "failure into a recovery strategy (remap to the fresh ref, "
        "dismiss the overlay, solve the challenge, synthesize a new "
        "capability, or abort) and gets exactly one re-attempt. "
        "Repeated screenshot failures also trigger a Jev-scored restart "
        "from a recently visited working page.",
        ("heal", "recovery", "stale", "recover", "retry", "fix", "dead",
         "restart"),
    ),
    Topic(
        "no-key-offline",
        "The system degrades gracefully: no TYPESAFE key means Jev "
        "answers fall back to a deterministic heuristic (confidence 0, "
        "nothing gets clicked — the loop stays testable offline). No "
        "GROQ key means no run starts at all. No Jina key means page "
        "memory runs at the anonymous 20 RPM limit with fail-soft rerank.",
        ("offline", "no key", "without api", "fallback", "heuristic",
         "keys", "credentials"),
    ),
    Topic(
        "install",
        "Install: clone the repo, run uv sync, copy .env.example to "
        ".env.local and fill GROQ_API_KEY + TYPESAFE_API_KEY, then "
        "uv run otc.py --url https://example.com --preflight to prove "
        "the keys and the headed browser both resolve. Solvers are "
        "optional add-ons (each has its own env key); unconfigured "
        "ones refuse loudly and cost nothing.",
        ("install", "setup", "getting started", "dependencies", "uv",
         "environment", "keys"),
    ),
    Topic(
        "usage",
        "Usage: uv run otc.py --url <start> --task \"plain English goal\" "
        "--budget 120 --max-steps 20. While it runs you can steer it "
        "from another terminal by appending lines to steer.txt "
        "(stop, goto <url>, or any instruction). --headless hides the "
        "window; the default is headed so you can watch and keep your "
        "hands off the mouse.",
        ("usage", "how do i run", "command", "cli", "steer", "headless",
         "run it"),
    ),
    Topic(
        "cost",
        "About $0.0002 per step: one Jev decision (fraction of a cent), "
        "an optional small writer call only when fresh text is needed, "
        "and deterministic DOM reading that costs nothing. Slow pages "
        "idle on wait without counting toward the stop rule, so a "
        "spinner never burns a step.",
        ("cost", "price", "cheap", "expensive", "per step", "spend",
         "billing"),
    ),
    Topic(
        "stop-rules",
        "Honest stopping: done is only accepted on a settled page with "
        "real text; two identical settled steps in a row stop the run "
        "(the action changed nothing); budgets (wall-clock + steps) cap "
        "it; CAPTCHA failure after the ledger-ordered plan stops it "
        "rather than looping. Stop rails scale with --max-steps.",
        ("stop", "halt", "end", "finish", "stall", "when does it stop",
         "done"),
    ),
    Topic(
        "limitations",
        "Known limits: the element map only sees the DOM (canvas/"
        "icon-only controls are invisible), slow pages burn wall-clock, "
        "and the headed browser fights you for the mouse while it runs. "
        "Passwords are only filled from {ENV} placeholders in --task — "
        "the writer never invents credentials.",
        ("limitation", "limit", "problem", "can't", "cannot", "blind",
         "shortcoming", "weakness"),
    ),
    Topic(
        "typesafe-slice",
        "Recent update: the model transport is now the official "
        "pydantic-ai-slim[typesafe] SDK client instead of hand-rolled "
        "HTTP — same protocol, SDK retries, and each run records the "
        "versioned model id that answered (run.json → jev_model). "
        "The shape follows the pydantic-ai contract: state holds "
        "judged material only, one judgment per question, confidence "
        "gates stay empirically tuned.",
        ("typesafe", "sdk", "transport", "pydantic", "official", "client"),
    ),
    Topic(
        "recent-updates",
        "What was updated recently: (1) crawl-frontier ledger so long "
        "runs never re-click visited targets; (2) solver ledger ranks "
        "bot-check backends by real past success; (3) Jev-scored "
        "recovery + restart from failures; (4) mission resume-until-"
        "done driver; (5) official SDK transport; (6) humanize "
        "smoothing default OFF after measured degradation; (7) "
        "clear-before-type so typing always replaces; (8) steps got "
        "3-4x faster (from ~17s to ~4-6s).",
        ("recent", "new", "updated", "changed", "what's new", "whats new",
         "latest", "improvement", "history"),
    ),
]


def _build_index() -> dict[str, int]:
    idx: dict[str, int] = {}
    for i, t in enumerate(TOPICS):
        for trig in t.triggers:
            idx[trig.strip().lower()] = i
    return idx


_INDEX = _build_index()

_WORD_RE = re.compile(r"[a-z0-9']+")


def _words(q: str) -> list[str]:
    return _WORD_RE.findall(q.lower())


def match(query: str) -> list[Topic]:
    """Keyword-route a free-English question to the best topics.

    Score = number of trigger phrases found (multi-word triggers count
    more), ties broken by topic order. Returns the top topics; at least
    the index hint when nothing matches, so a caller can always answer.
    Pure, no I/O, no model.
    """
    text = (query or "").lower()
    scores = [0] * len(TOPICS)
    for trigger, i in _INDEX.items():
        if trigger in text:
            scores[i] += 1 + (len(trigger) - 1)
    best = [i for i, s in enumerate(scores) if s > 0]
    if not best:
        return []
    # keep stable order among the matched topics
    return [TOPICS[i] for i in sorted(best)]


def _index_lines() -> str:
    lines = [f"  {t.key}" for t in TOPICS]
    return "\n".join(lines)


def answer(query: str, full: bool = False) -> str:
    """Answer a free-English question with plain-English topics.

    ``full=True`` returns the selected topics' details; default returns
    summaries only. No-match returns the topic index so the questioner
    can ask again by topic key.
    """
    hits = match(query)
    if not hits:
        return (
            "I couldn't match that to a capability. "
            "I can explain these — try one of the keys (or ask it in plain English):\n"
            + _index_lines()
        )
    if full:
        return "\n\n".join(str(t) for t in hits)
    return "\n\n".join(t.summary for t in hits)


def list_topics() -> str:
    return _index_lines()


def topic_names() -> list[str]:
    return [t.key for t in TOPICS]


def load_env_file(path: str = ".env.local") -> dict[str, str]:
    """Tiny .env.local reader used only by the key-status reporter.

    Returns {NAME: value-or-'(set)'} for lines NAME=value; values that
    look like secrets are masked. Never writes, never fails on a missing
    file.
    """
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name):
                continue
            out[name] = value.strip() or "(empty)"
    return out


_SECRET_MARK = {
    "GROQ_API_KEY", "TYPESAFE_API_KEY", "CAPTCHA_KRAKEN_API_KEY",
    "APIKEY_2CAPTCHA", "JINA_API_KEY",
}


def key_status(path: str = ".env.local") -> str:
    """Which API keys are present in .env.local (values masked, never printed)."""
    vals = load_env_file(path)
    import os as _os
    lines: list[str] = []
    for name in sorted(_SECRET_MARK):
        v = vals.get(name)
        if v is None:
            live = bool(_os.environ.get(name))
            lines.append(f"  {name}: {'(in current env)' if live else 'missing'}")
        else:
            lines.append(f"  {name}: {'set (masked)' if v not in ('(empty)',) else 'empty'}")
    return "\n".join(lines)
