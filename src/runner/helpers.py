"""runner.helpers — pure helper functions used by the step loop.

Each function is deterministic and side-effect-free; they operate on
plain data structures (strings, tuples, element lists) so they can be
unit-tested in isolation.
"""

from __future__ import annotations

import re
from typing import Any

from .. import perception
from ..deps import ElementRef
from .constants import CAPTCHA_MAX_ATTEMPTS, STOP_AFTER_NOOPS


def page_settled(page_text: str) -> bool:
    """False while the page is blank or shows a loading indicator.

    A long page that merely *contains* a banner like "Looking for results
    in English?" (language prompt) alongside real results counts as settled —
    only a short/blank page dominated by the indicator blocks done.
    """
    text = (page_text or "").strip()
    if not text:
        return False
    if len(text) >= 300:
        return True
    return not _LOADING_RE.search(text)


_LOADING_RE = re.compile(r"looking for results|loading|^\s*$", re.IGNORECASE)


def loop_guard_trip(last_sig: tuple | None, run: int, sig: tuple) -> tuple[bool, int]:
    """True when the same (kind, item, url) repeats 3+ guardable steps in a row.

    The execution layer then forces a wait instead of re-issuing the action:
    fixation breaks mechanically even when the classifier stays confident.
    Waits and other kinds leave the counters untouched, so three identical
    clicks/types trip it even with waits interleaved; any navigation (url in
    the sig) or different target resets the run.
    """
    run = run + 1 if sig == last_sig else 1
    return run >= 3, run


def escalation_target(mismatch_run: int, candidate_url: str | None,
                      current_url: str, escalated: set[str]) -> str | None:
    """Writer-proposed URL to escalate to after repeated identical mismatches.

    When the classifier fixates on a mismatched pick, the structurally sound
    move is a destination goto — not another idle, not a low-approval click
    on a possibly-wrong target. Fires once per distinct URL, never the page
    we're already on; anything else returns None (keep waiting / stop).
    """
    if mismatch_run < 3 or not candidate_url:
        return None
    target = perception.norm_url(candidate_url)
    if not target or target in escalated:
        return None
    if target == perception.norm_url(current_url):
        return None
    return candidate_url


_CHALLENGE_KINDS = ("checkbox", "slider")

_CHALLENGE_MARKERS = (
    "captcha", "recaptcha", "puzzle", "verify you are human",
    "i'm not a robot", "not a robot", "slide to", "drag", "checkbox",
    "select all images", "select each image", "image grid",
)


def heal_strategy_fits(strategy_value: str, target_kind: str,
                       label: str = "") -> bool:
    """True when a heal strategy fits the failed target.

    ``drag_slider`` / ``solve_challenge`` dispatch into
    ``challenge_control``, which refuses anything that isn't a
    checkbox/slider/captcha control — sending a plain link, button,
    input, or a gone box there always ends in a refusal ("not a
    checkbox/slider ... no dispatch"). Those mistriages must fall back
    before dispatching, not after failing. All other strategies fit
    every target. Pure.
    """
    if strategy_value not in ("drag_slider", "solve_challenge"):
        return True
    if (target_kind or "") in _CHALLENGE_KINDS:
        return True
    blob = f"{target_kind or ''} {label or ''}".lower()
    return any(marker in blob for marker in _CHALLENGE_MARKERS)


def stop_limits(max_steps: int) -> tuple[int, int, int]:
    """(noop, dead-run, fixation) stop limits scaled to the step budget.

    A 3-no-op guillotine fits a 50-step task, not a 1000-step mission:
    long runs must survive transient stalls while true fixation still ends
    them, proportionally. Base values preserved at default budgets.
    """
    steps = max(1, int(max_steps))
    return (max(STOP_AFTER_NOOPS, steps // 50),
            max(2, steps // 100),
            max(6, steps // 50))


def restart_candidates(visited: list[str], current_url: str,
                       limit: int = 5) -> list[str]:
    """Most-recent distinct visited URLs excluding the current page.

    Restart ranking pool for decide_restart_action: known-alive pages
    first, bounded so the Choice stays small.
    """
    out: list[str] = []
    for url in reversed(visited or []):
        if url and url != current_url and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def screenshot_should_restart(streak: int) -> bool:
    """Restart attempts at every 3rd consecutive capture failure (3, 6, 9)."""
    return streak >= 3 and (streak - 3) % 3 == 0 and streak < 12


def screenshot_dead(streak: int) -> bool:
    """The capture pipe is unrecoverable: stop honestly, mission relaunches."""
    return streak >= 12


def classify_noop(act: str, result: Any = None, *, blank: bool = False,
                  blocked: str | None = None, same_fp: bool = False) -> str:
    """Map a noop step record to a failure reason for the recovery cycle.

    Reasons: blank (nothing to work with), blocked (bot-check page —
    challenge flow owns it), fixation (loopguard repeat), mismatch
    (escalation owns it), lowconf, notready, cooldown, covered (overlay —
    dismiss it), failed-same / failed (action error, print equal or not),
    idle (patience paths), unknown. Pure; first match wins.
    """
    a = act or ""
    r = result if isinstance(result, str) else ""
    if blank:
        return "blank"
    if blocked:
        return "blocked"
    if a.startswith("loopguard wait"):
        return "fixation"
    if a.startswith("mismatch wait"):
        return "mismatch"
    if a.startswith("idle (low confidence)"):
        return "lowconf"
    if a.startswith("DONE rejected"):
        return "notready"
    if a.startswith("goto rejected"):
        return "cooldown"
    if "covered:" in a or "covered:" in r:
        return "covered"
    if r.startswith("error") or "error:" in r:
        return "failed-same" if same_fp else "failed"
    if a in ("wait", "none") or a.startswith("type_at #") or a in (
            "challenge (no item)", "goto (no URL)"):
        return "idle"
    return "unknown"


def step_verdict(*, done: bool, stopped: bool, acted: bool,
                 recovered: bool, unresolved: bool) -> str:
    """Per-step outcome in the contract taxonomy: complete / terminal /
    progress / recoverable / unresolved / noop. Recorded on every entry."""
    if done:
        return "complete"
    if stopped:
        return "terminal"
    if acted:
        return "progress"
    if recovered:
        return "recoverable"
    if unresolved:
        return "unresolved"
    return "noop"


def captcha_should_stop(streak: int, cap: int = CAPTCHA_MAX_ATTEMPTS) -> bool:
    """True when image-challenge dead-ends exhaust the attempt budget."""
    return streak >= cap


def fresh_tabs(known: set[int], current: set[int]) -> set[int]:
    """Tabs the loop hasn't adopted yet (SEE-time reconciliation).

    click_item adopts 0.7s after its click, but slow popups register later.
    Any tab unknown at SEE time gets adopted so perception never strands on
    a stale tab while a fresh result sits unopened beside it.
    """
    return current - known


def is_blank_page(elements: list, page_text: str) -> bool:
    """True when SEE found nothing to work with (dead load, blank tab).

    A stuck loader draws confident waits forever; the runner answers with
    a periodic refresh instead (see the blank-refresh override).
    """
    return not elements and len((page_text or "").strip()) < 50


def no_effect_trip(prev_acted: bool, prev_fp: tuple | None, fp: tuple,
                   settled: bool, last_effect_kind: str | None) -> bool:
    """True when the previous step acted yet the page is observably identical.

    Strictly consecutive only: idle/wait/gate steps neither advance nor
    reset the count — they are patience, and only back-to-back dead actions
    end the run. (Loop-guard separately covers repeated identical intents.)
    Synthesized heal_step<N> capabilities are effectful by construction.
    """
    from .constants import EFFECT_KINDS
    return bool(prev_acted and prev_fp is not None and fp == prev_fp
                and settled
                and (last_effect_kind in EFFECT_KINDS
                     or str(last_effect_kind or "").startswith("heal_step")))


def notes_url_count(notes: list[str]) -> int:
    """Distinct page URLs banked in the notes buffer (notes store norms)."""
    urls = set()
    for line in notes or []:
        url = line.split(" :: ", 1)[0].strip()
        if url:
            urls.add(url)
    return len(urls)


def should_submit_instead(idx: int, elements: list[ElementRef],
                          last_typed: tuple | None, url: str,
                          live_value: str = "") -> bool:
    """True when type_at targets the already-filled field it just typed.

    Retyping replaces identical text (clear-before-type) — nothing changes.
    Pressing Enter submits the standing query instead. Scoped to the same
    element on the same URL so multi-field forms are unaffected. live_value
    is a freshly read DOM value for pages whose snapshots hide fill-state.
    """
    if last_typed is None or (idx, url) != last_typed:
        return False
    el = next((e for e in elements if e.idx == idx), None)
    if el is None:
        return False
    return el.value_len > 0 or bool((live_value or "").strip())


def credential_placeholder(task: str, *, want_password: bool) -> str | None:
    """Pick a {ENV} placeholder from the task for a credential field."""
    names = re.findall(r"\{([A-Z_][A-Z0-9_]*)\}", task or "")
    if not names:
        return None
    if want_password:
        for n in names:
            if any(k in n for k in ("PASSWORD", "PASSWD", "PASS", "PWD")):
                return "{" + n + "}"
        return "{" + names[-1] + "}"
    for n in names:
        if any(k in n for k in ("USER", "EMAIL", "LOGIN", "NAME")):
            return "{" + n + "}"
    return "{" + names[0] + "}"
