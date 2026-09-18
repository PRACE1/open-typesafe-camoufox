"""stage.py — challenge STAGE classifier (pure; no browser).

Port of the classification core of
https://github.com/BetterWright/betterwright/blob/main/src/captcha-solver.ts
(``classifyChallengeStage`` + provider/stage tables + instruction regexes).
No code copied — reimplemented in Python against our metadata shape.
Their license situation is unverified, so this is clean-room behavior
parity, not a vendor.

Why this exists alongside ``shield_solve.detect_shield``: detect_shield
answers "which token family?" (checkbox click + poll). This answers
"what interactive STAGE is on screen right now?" — checkbox, turnstile
widget, managed Cloudflare wall, open image grid, slider, text captcha,
invisible widget — plus provider and whether the stage is natively
auto-solvable or needs vision. The runner logs one line per attempt
("show each"), and ``runner.policy`` drives the next action off it.

Priority rules (ported faithfully):
- Child frames first: host-page marketing copy must not override a live
  provider widget.
- Hidden frames skipped: preloaded dormant providers are not a stage
  until visible or explicitly blocking.
- Instruction copy beats a generic challenge-frame URL (hCaptcha serves
  motion, drag and grids from one widget).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

# -- stages -----------------------------------------------------------------

NONE = "none"
CHECKBOX = "checkbox"
TURNSTILE = "turnstile"
MANAGED = "managed_challenge"
IMAGE_GRID = "image_grid"
MOTION = "motion"
SLIDER = "slider"
TEXT = "text"
INVISIBLE = "invisible"
UNKNOWN = "unknown"

STAGES = (NONE, CHECKBOX, TURNSTILE, MANAGED, IMAGE_GRID, MOTION,
          SLIDER, TEXT, INVISIBLE, UNKNOWN)

PROVIDERS = ("recaptcha", "hcaptcha", "turnstile", "cloudflare", "bing",
             "google", "generic")

AUTO_SOLVABLE = frozenset({CHECKBOX, TURNSTILE, MANAGED, SLIDER, MOTION})
NEEDS_VISION = frozenset({IMAGE_GRID, TEXT})

# -- instruction regexes (ported from their *_TEXT tables) -------------------

IMAGE_GRID_TEXT = re.compile(
    r"(?:please )?(?:click|select|choose|tap) (?:all|each|every|the) "
    r"(?:image|images|square|squares|tile|tiles)\b"
    r"|select all images|which of these|pick each image|contains a \w+",
    re.IGNORECASE)
MOTION_TEXT = re.compile(
    r"(?:shape|object|item) that (?:grows|moves|animates|changes|shrinks)"
    r"|click on the (?:moving|growing) (?:shape|object|item)"
    r"|please click on the shape",
    re.IGNORECASE)
SLIDER_TEXT = re.compile(
    r"(?:slide|drag|swipe|move).{0,60}(?:slider|puzzle|piece|handle|arrow|element|shape)"
    r"|drag the (?:element|piece|shape|object)"
    r"|complete the (?:puzzle|slider)",
    re.IGNORECASE)
TEXT_CAPTCHA_TEXT = re.compile(
    r"(?:type|enter|fill).{0,30}(?:characters|code|text|letters|numbers)"
    r"|what (?:code|text|characters)",
    re.IGNORECASE)
CHECKBOX_TEXT = re.compile(
    r"i(?:'|’)?m not a robot|verify you are human"
    r"|confirm you are (?:a )?human|i am human",
    re.IGNORECASE)
MANAGED_TEXT = re.compile(
    r"checking your browser|just a moment|performing security verification"
    r"|enable javascript and cookies|ddos protection by cloudflare"
    r"|attention required",
    re.IGNORECASE)


def _norm(text: str) -> str:
    """Lowercase + collapse whitespace (their normalizedText)."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _host(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def _path(url: str) -> str:
    try:
        return (urlsplit(url or "").path or "").lower()
    except ValueError:
        return ""


def _host_is(host: str, domain: str) -> bool:
    host = (host or "").lower().lstrip(".")
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def provider_from_url(url: str) -> str:
    """Provider from a frame/page URL (their providerFromUrl)."""
    host = _host(url)
    path = _path(url)
    if not host:
        return "generic"
    if _host_is(host, "hcaptcha.com"):
        return "hcaptcha"
    if _host_is(host, "cloudflare.com"):
        return "turnstile" if "turnstile" in path else "cloudflare"
    if _host_is(host, "google.com") or _host_is(host, "recaptcha.net"):
        return "recaptcha" if "/recaptcha/" in path else "google"
    if _host_is(host, "bing.com"):
        return "bing"
    return "generic"


class StageResult(dict):
    """Classification outcome: stage/provider/signal/source + routing flags."""

    @property
    def stage(self) -> str:
        return str(self.get("stage", NONE))

    @property
    def provider(self) -> str:
        return str(self.get("provider", "generic"))

    @property
    def auto_solvable(self) -> bool:
        return bool(self.get("auto_solvable", False))

    @property
    def needs_vision(self) -> bool:
        return bool(self.get("needs_vision", False))


def classify_stage(metadata: dict[str, Any] | None = None) -> StageResult:
    """Classify the interactive challenge stage from page metadata.

    ``metadata`` shape (all keys optional, fail-soft)::
        {"url": ..., "title": ..., "text": ...,
         "provider": ..., "type": ...,
         "frames": [{"url":..., "title":..., "text":..., "visible":...}, ...]}

    Pure function — no browser, fully unit-testable.
    """
    meta = metadata if isinstance(metadata, dict) else {}
    raw_frames = meta.get("frames")
    frames = [f for f in (raw_frames or []) if isinstance(f, dict)]
    sources = []
    for i, f in enumerate(frames):
        vis = f.get("visible")
        sources.append({
            "kind": "frame", "index": i,
            "url": str(f.get("url") or ""),
            "title": str(f.get("title") or ""),
            "text": str(f.get("text") or ""),
            "visible": vis if isinstance(vis, bool) else None,
        })
    sources.append({
        "kind": "main", "index": None,
        "url": str(meta.get("url") or ""),
        "title": str(meta.get("title") or ""),
        "text": str(meta.get("text") or ""),
        "visible": True,
    })

    provider = str(meta.get("provider") or "generic")
    stage = NONE
    signal: str | None = None
    source = sources[-1]

    for cand in sources:
        # Dormant preloaded providers are not a stage until visible.
        if cand["kind"] == "frame" and cand["visible"] is False:
            continue
        text = _norm(f"{cand['title']}\n{cand['text']}")
        url = cand["url"].lower()
        path = _path(cand["url"])
        from_url = provider_from_url(cand["url"])
        if from_url != "generic":
            provider = from_url

        if MOTION_TEXT.search(text):
            stage, signal, source = MOTION, "motion", cand
            break
        if IMAGE_GRID_TEXT.search(text):
            stage, signal, source = IMAGE_GRID, "image_grid", cand
            break
        if (stage in (NONE, UNKNOWN) and
                (SLIDER_TEXT.search(text) or
                 re.search(r"slider|puzzle-captcha|geetest", text))):
            stage, signal, source = SLIDER, "slider", cand
            if cand["kind"] == "frame":
                break
            continue
        if ("/bframe" in path or
                re.search(r"[?&#]frame=challenge(?:[&#]|$)", url) or
                re.search(r"/hcaptcha-?challenge", path)):
            stage, signal, source = IMAGE_GRID, "image_grid", cand
            break
        if "/cdn-cgi/challenge-platform/" in path and "/turnstile/" not in path:
            stage, signal, source = MANAGED, "cloudflare_managed", cand
            if provider == "generic":
                provider = "cloudflare"
            if cand["kind"] == "frame":
                continue  # keep scanning for a grid escalation
            break
        if MANAGED_TEXT.search(text) and stage == NONE:
            stage, signal, source = MANAGED, "managed_text", cand
            if from_url in ("cloudflare", "turnstile"):
                provider = from_url
            if cand["kind"] == "frame":
                continue
            break
        if ("/turnstile/" in path or
                _host_is(_host(cand["url"]), "challenges.cloudflare.com")):
            if stage in (NONE, CHECKBOX, UNKNOWN, INVISIBLE):
                stage, signal, source = TURNSTILE, "turnstile_widget", cand
                provider = "turnstile"
            continue  # keep scanning for grid escalation only
        if (stage == NONE and TEXT_CAPTCHA_TEXT.search(text) and
                re.search(r"captcha|challenge|security", text)):
            stage, signal, source = TEXT, "text_challenge", cand
            if cand["kind"] == "frame":
                break
            continue
        if (path.endswith("/anchor") or
                re.search(r"[?&#]frame=checkbox(?:[&#]|$)", url) or
                CHECKBOX_TEXT.search(text)):
            if stage in (NONE, UNKNOWN):
                stage, signal, source = CHECKBOX, "checkbox", cand
            continue
        if (re.search(r"(?:^|[?&#])size=invisible(?:[&#]|$)", url) or
                "checkbox-invisible" in url):
            if stage == NONE:
                stage, signal, source = INVISIBLE, "invisible_widget", cand

    if stage == NONE and (meta.get("provider") or meta.get("type") == "bot_challenge"):
        stage, signal = UNKNOWN, "unclassified"

    return StageResult({
        "stage": stage, "provider": provider, "signal": signal,
        "source": {"kind": source.get("kind") or "main",
                   "url": source.get("url") or "",
                   "index": source.get("index")},
        "auto_solvable": stage in AUTO_SOLVABLE,
        "needs_vision": stage in NEEDS_VISION,
    })


async def collect_metadata(page, page_text: str = "",
                           title: str = "") -> dict[str, Any]:
    """Build ``classify_stage`` metadata from the live page (fail-soft).

    Frames probed: recaptcha anchor + bframe, hCaptcha, Turnstile —
    URL + title + visibility each (never inner text: cross-origin
    frames expose none). Never raises; missing pieces become "".
    """
    frames: list[dict[str, Any]] = []
    try:
        page_url = page.url
    except Exception:  # noqa: BLE001
        page_url = ""
    for sel in (
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='turnstile']"):
        try:
            loc = page.locator(sel)
            try:
                import asyncio as _asyncio

                count = await _asyncio.wait_for(loc.count(), timeout=5.0)
            except Exception:  # noqa: BLE001
                continue
            for i in range(min(count, 4)):
                item = loc.nth(i)
                try:
                    import asyncio as _asyncio

                    src = await _asyncio.wait_for(
                        item.get_attribute("src"), timeout=5.0) or ""
                    ttl = await _asyncio.wait_for(
                        item.get_attribute("title"), timeout=5.0) or ""
                    vis = await _asyncio.wait_for(
                        item.is_visible(timeout=500), timeout=5.0)
                except Exception:  # noqa: BLE001
                    continue
                frames.append({"url": src, "title": ttl, "text": "",
                               "visible": bool(vis)})
        except Exception:  # noqa: BLE001
            continue
    return {"url": page_url or "", "title": title or "",
            "text": page_text or "", "frames": frames}
