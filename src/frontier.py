"""frontier.py — persistent crawl-frontier ledger for long collection runs.

Problem it solves (seen live): on a 200-step mission the agent
re-opens the same links (APHCI, Amalgamated, Plumbing in Ireland) over
and over. History windows (8 lines) and notes forget; Google wraps SERP
hrefs in opaque ``/goto?url=`` tokens so URL matching never fires; and
the approval-override path lets the writer re-propose visited targets.

The Frontier is the run's to-do list, keyed to survive all three:

* **Discovery**: every SEE banks link targets as ``pending``.
* **Identity without URLs**: entries key on ``(host, normalized label)``
  because SERP hrefs are opaque — the visible result title ("APHCI")
  is stable across probes while ``#34``/``#38`` idx churns.
* **Visit learning**: each successful navigation records a
  label→URL alias, so the *next* SERP probe of the same group resolves
  to visited even though its href is still opaque.
* **Seeding**: known URLs (mission ``seed_visited``, steer-file group
  lists) mark entries visited from step 0 — no re-clicks, ever.
* **Persistence**: append-only ``frontier.jsonl`` event ledger (the
  canonical record the user asked for) plus a ``frontier.json``
  snapshot for cheap resume and human inspection.

Pure logic, no browser, no network — fully unit-testable offline.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field

from .perception import host_of, norm_url

_URL_RE = re.compile(r"https?://[^\s\"'<>|]+")
# Best-effort "Name 3.6K https://..." triples as written in steer.txt:
# a label, a member/count token, then the URL. Trailing punctuation
# (;,.) stripped from the captured URL.
_PAIR_RE = re.compile(
    r"([^;]+?)\s+[\d.,]+\s*[KkMm]?\s*(https?://[^\s\"'<>|;,]+)"
)

_PENDING = "pending"
_VISITED = "visited"

# Element kinds that can navigate somewhere worth ledgering. Buttons
# like "Search" would pollute the queue, so only links are observed.
LEDGER_KINDS = ("link",)


def normalize_label(text: str | None) -> str:
    """Canonical label identity: lowercase, collapsed whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _leading_tokens(s: str) -> list[str]:
    """Alphanumeric head tokens for overlap matching."""
    return re.findall(r"[a-z0-9]+", s)


def labels_match(a: str, b: str, min_prefix: int = 5) -> bool:
    """True when two label norms name the same target.

    Exact equality first; otherwise either (a) a head-prefix match —
    SERP result titles front-load the stable name (title, then URL,
    then site and snippet tail), so drift happens at the TAIL while
    the head stays put ("APHCI Facebook https://…" vs "APHCI"); or
    (b) 3+ shared leading tokens — re-renders that diverge mid-title
    ("… Dublin" vs "… Dublin latest updates" vs "… Dublin see more").
    The prefix rule needs ``min_prefix`` chars so generic chrome
    ("Home", "Join", "More", "Back") never matches; the token rule
    needs 3+ shared head tokens so "Plumbing in Ireland" vs "Plumbers
    in Ireland Jobs" (zero shared head tokens) stays distinct.
    Deliberately head-anchored, never either-way containment: tails
    never identify.
    """
    if not a or not b:
        return False
    if a == b:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= min_prefix and long.startswith(short):
        return True
    ta, tb = _leading_tokens(a), _leading_tokens(b)
    shared = 0
    for x, y in zip(ta, tb):
        if x != y:
            break
        shared += 1
    return shared >= 3


def element_label(e) -> str:
    """Best human label for an element (mirrors prompt rendering)."""
    return e.label or e.placeholder or e.text or e.id or ""


def extract_urls(text: str) -> list[str]:
    """All bare URLs in a text blob (steer lines, notes, rationales)."""
    out = []
    for m in _URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(").,;")
        if url not in out:
            out.append(url)
    return out


def extract_seed_pairs(text: str) -> list[tuple[str, str]]:
    """(label, url) pairs from "Name count URL" runs (steer.txt style).

    Lets a mission skip list written as prose ("APHCI 652
    https://...") seed the ledger with real label→URL aliases from
    step 0, before any navigation teaches them.
    """
    out = []
    for m in _PAIR_RE.finditer(text or ""):
        label = normalize_label(m.group(1).split(":")[-1])
        url = m.group(2).rstrip(").,;")
        if label and url and (label, url) not in out:
            out.append((label, url))
    return out


@dataclass
class FrontierEntry:
    """One ledgered link target."""

    key: str = ""
    url: str = ""
    label: str = ""
    host: str = ""
    status: str = _PENDING
    first_seen_step: int = 0
    last_seen_step: int = 0
    visits: int = 0
    source: str = "serp"
    title: str = ""


class Frontier:
    """Ordered to-do ledger of link targets for one mission."""

    def __init__(self) -> None:
        self.entries: dict[str, FrontierEntry] = {}
        self.order: list[str] = []
        self.dead: dict[str, dict] = {}
        self._pending_events: list[dict] = []

    # -- identity ----------------------------------------------------

    @staticmethod
    def _url_key(url: str) -> str:
        return f"url:{norm_url(url)}" if url else ""

    @staticmethod
    def _label_key(label: str, host: str = "") -> str:
        norm = normalize_label(label)
        return f"label:{host}|{norm}" if norm else ""

    def _is_opaque_href(self, href: str) -> bool:
        """True when the href hides its destination (redirect wrappers)."""
        try:
            h = norm_url(href)
        except Exception:
            return True
        # Opaque when the normalized form carries no distinguishing path
        # beyond a token (Google /goto?url=, /url?q= with stripped q).
        return h.startswith("https://www.google.com/goto") or (
            "/url?" in h and "q=http" not in h)

    # -- observation ---------------------------------------------------

    def observe(self, elements, step: int, url_now: str = "") -> int:
        """Bank unseen link targets from a SEE probe. Returns new count."""
        new = 0
        for e in elements or []:
            if getattr(e, "kind", "") not in LEDGER_KINDS:
                continue
            label = element_label(e)
            if not label:
                continue
            href = getattr(e, "href", "") or ""
            host = host_of(href)
            key = self._label_key(label, host)
            if not key or key in self.entries:
                if key and key in self.entries:
                    self.entries[key].last_seen_step = step
                continue
            # A real (non-opaque) href that is already visited aliases
            # straight to visited instead of queueing.
            if href and not self._is_opaque_href(href):
                ukey = self._url_key(href)
                if ukey in self.entries and \
                        self.entries[ukey].status == _VISITED:
                    alias = self.entries[ukey]
                    self.entries[key] = FrontierEntry(
                        key=key, url=alias.url, label=label, host=host,
                        status=_VISITED, first_seen_step=step,
                        last_seen_step=step, visits=alias.visits,
                        source="alias")
                    self.order.append(key)
                    self._pending_events.append({
                        "event": "aliased", "key": key,
                        "url": alias.url, "label": label, "step": step})
                    continue
            self.entries[key] = FrontierEntry(
                key=key, url=href, label=label, host=host,
                status=_PENDING, first_seen_step=step,
                last_seen_step=step, source="serp")
            self.order.append(key)
            self._pending_events.append({
                "event": "discovered", "key": key, "url": href,
                "label": label, "step": step})
            new += 1
        return new

    # -- visit accounting ------------------------------------------------

    def mark_visited(self, url: str, label: str = "",
                     title: str = "", step: int = 0,
                     quiet: bool = False) -> bool:
        """Record a successful navigation. Returns True on first visit.

        Marks by normalized URL *and* by label alias, so opaque SERP
        hrefs pointing at the same destination resolve as visited next
        probe. Re-visits bump the counter (loop signal) but stay visited.
        ``quiet`` (used by seed re-runs) skips event emission and counter
        bumps for already-visited targets so the ledger isn't spammed.
        """
        norm = normalize_label(label)
        ukey = self._url_key(url)
        if quiet and self._is_known_visited(url, norm):
            return False
        first = True
        if ukey:
            ent = self.entries.get(ukey)
            if ent is None:
                self.entries[ukey] = FrontierEntry(
                    key=ukey, url=url, label=label,
                    host=host_of(url), status=_VISITED,
                    first_seen_step=step, last_seen_step=step,
                    visits=1, source="navigated",
                    title=(title or "")[:120])
                self.order.append(ukey)
            else:
                first = ent.visits == 0 and ent.status != _VISITED
                ent.status = _VISITED
                ent.visits += 1
                ent.last_seen_step = step
                if label and not ent.label:
                    ent.label = label
                if title:
                    ent.title = (title or "")[:120]
            self._pending_events.append({
                "event": "visited", "key": ukey, "url": url,
                "label": label, "step": step})
        if norm:
            host = host_of(url)
            lkey = self._label_key(label, host)
            if lkey and lkey not in self.entries:
                self.entries[lkey] = FrontierEntry(
                    key=lkey, url=url, label=label, host=host,
                    status=_VISITED, first_seen_step=step,
                    last_seen_step=step, visits=1, source="alias",
                    title=(title or "")[:120])
                self.order.append(lkey)
                self._pending_events.append({
                    "event": "aliased", "key": lkey, "url": url,
                    "label": label, "step": step})
            elif lkey:
                ent = self.entries[lkey]
                first = first and ent.visits == 0 and \
                    ent.status != _VISITED
                ent.status = _VISITED
                ent.visits += 1
                ent.last_seen_step = step
                if url and not ent.url.startswith("http"):
                    ent.url = url
        return first

    def _is_known_visited(self, url: str, norm_label: str) -> bool:
        """True when the URL or label is already ledgered as visited."""
        if url:
            ent = self.entries.get(self._url_key(url))
            if ent is not None and ent.status == _VISITED:
                return True
        if norm_label:
            for ent in self.entries.values():
                if ent.status == _VISITED and labels_match(
                        normalize_label(ent.label), norm_label):
                    return True
        return False

    def seed_urls(self, urls, step: int = 0) -> int:
        """Mark known URLs visited (mission seed_visited, steer links)."""
        n = 0
        for url in urls or []:
            if self.mark_visited(url, label="", step=step, quiet=True):
                n += 1
        return n

    def seed_pairs(self, pairs, step: int = 0) -> int:
        """Seed (label, url) aliases from prose skip-lists (steer.txt)."""
        n = 0
        for label, url in pairs or []:
            if self.mark_visited(url, label=label, step=step, quiet=True):
                n += 1
        return n

    # -- queries for prompts/guards ---------------------------------------

    def is_visited_element(self, e) -> bool:
        """True when this element must not be proposed again.

        Matches already-read targets (by real href, then by normalized
        label — the label path is what kills SERP re-clicks through
        opaque Google redirect hrefs) AND dead targets (acted on
        repeatedly with no observable effect). Both are routing
        signals; the prompt suffix (VISITED vs DEAD) tells models why.
        """
        try:
            if self.is_dead_element(e):
                return True
        except Exception:
            pass
        href = getattr(e, "href", "") or ""
        if href and not self._is_opaque_href(href):
            ent = self.entries.get(self._url_key(href))
            if ent is not None and ent.status == _VISITED:
                return True
        label = element_label(e)
        if not label:
            return False
        host = host_of(href)
        ent = self.entries.get(self._label_key(label, host))
        if ent is not None and ent.status == _VISITED:
            return True
        # Host-agnostic fallback: a visited label under any host counts.
        # Head-prefix matching (not equality): SERP probes re-render the
        # same title with drifting snippet tails, and steer seeds carry
        # short names ("aphci") against long probe titles.
        norm = normalize_label(label)
        for ent in self.entries.values():
            if ent.status == _VISITED and labels_match(
                    normalize_label(ent.label), norm):
                return True
        return False

    def is_visited_url(self, url: str) -> bool:
        """True when this exact normalized URL was already read."""
        if not url:
            return False
        try:
            ent = self.entries.get(self._url_key(url))
        except Exception:
            return False
        return ent is not None and ent.status == _VISITED

    # -- dead-action marks ---------------------------------------------------
    # A visited entry means "read it". A dead mark means "acted on it and
    # nothing observably changed" — repeat the action only with new
    # information. Keyed by action kind + host + normalized label: host
    # keeps generic labels ("Next") site-scoped, while href-less widgets
    # (bot-check checkboxes) stay dead across same-host pages and even
    # sessions via the persisted snapshot. Thresholded at 2: one dead
    # act may be transient.

    DEAD_THRESHOLD = 2

    @staticmethod
    def _dead_key(kind: str, label: str, host: str = "") -> str:
        return f"dead:{kind}|{host}|{normalize_label(label)}"

    def mark_dead(self, kind: str, label: str, host: str = "",
                  step: int = 0) -> int:
        """Record a no-effect action. Returns the dead count for the key."""
        norm = normalize_label(label)
        if not norm:
            return 0
        key = self._dead_key(kind, label, host)
        rec = self.dead.get(key, {"count": 0, "kinds": [],
                                  "last_step": 0})
        rec["count"] = int(rec.get("count", 0) or 0) + 1
        kinds = rec.get("kinds") or []
        if kind not in kinds:
            kinds.append(kind)
        rec["kinds"] = kinds
        rec["last_step"] = step
        self.dead[key] = rec
        return rec["count"]

    def dead_count(self, e=None, *, kind: str = "",
                   label: str = "", host: str = "") -> int:
        """Effectless-act count for an element or label (+host).

        Matches by head-prefix like everything else: the dead mark is
        recorded under the ACT label while later probes render the same
        target with drifted tails. Host scoping is opaqueness-aware: a
        redirect-wrapper href (Google /goto) carries no host identity,
        so it never vetoes on host grounds — but a REAL destination
        host that disagrees with the recorded one does.
        """
        probe_href = ""
        if e is not None:
            norm = normalize_label(element_label(e))
            probe_href = getattr(e, "href", "") or ""
            host = host_of(probe_href)
        else:
            norm = normalize_label(label)
        if not norm:
            return 0
        # Summed (not max): drifted re-marks of the same target land on
        # sibling keys, and futility is cumulative per target family.
        total = 0
        for key, rec in self.dead.items():
            parts = key.split("|")
            if len(parts) < 3 or not parts[0].startswith("dead:"):
                continue
            k_kind = parts[0][len("dead:"):]
            k_host = parts[1]
            k_norm = "|".join(parts[2:])
            if kind and k_kind != kind:
                continue
            if (host and k_host != host
                    and not self._is_opaque_href(probe_href)):
                continue
            if not labels_match(k_norm, norm):
                continue
            try:
                total += int(rec.get("count", 0) or 0)
            except (ValueError, TypeError):
                pass
        return total

    def is_dead_element(self, e, threshold: int = DEAD_THRESHOLD) -> bool:
        """True when this target died repeatedly: route around it."""
        try:
            return self.dead_count(e) >= threshold
        except Exception:
            return False

    def element_mark(self, e) -> str:
        """Prompt suffix for an element: "" | VISITED… | DEAD… notice."""
        if self.is_dead_element(e):
            n = self.dead_count(e)
            return (f"DEAD — acted on {n}x with no observable effect, "
                    f"do not pick again without new information")
        if self.is_visited_element(e):
            return "VISITED — already read, do not propose"
        return ""

    def clear_dead(self) -> None:
        """Drop all dead marks (fresh page context after navigation)."""
        self.dead = {}

    def _dead_norms(self) -> list[tuple[str, str]]:
        """(host, label) pairs at/over the dead threshold (for filtering)."""
        out = []
        for key, rec in self.dead.items():
            try:
                if int(rec.get("count", 0) or 0) < self.DEAD_THRESHOLD:
                    continue
            except (ValueError, TypeError):
                continue
            parts = key.split("|")
            if len(parts) >= 3:
                out.append((parts[1], "|".join(parts[2:])))
        return out

    def _is_dead_norm(self, host: str, norm: str, href: str = "") -> bool:
        for d_host, d_norm in self._dead_norms():
            if d_host != host and host and not self._is_opaque_href(href):
                continue
            if labels_match(d_norm, norm):
                return True
        return False

    def pending_labels(self, limit: int = 12) -> list[str]:
        """Oldest pending target labels for prompts (bounded).

        Dead targets are excluded: listing them as "unvisited to-do"
        would directly contradict their DEAD marks elsewhere.
        """
        out = []
        for key in self.order:
            ent = self.entries[key]
            if ent.status == _PENDING and ent.label:
                if self._is_dead_norm(ent.host,
                                      normalize_label(ent.label),
                                      ent.url):
                    continue
                out.append(ent.label)
                if len(out) >= limit:
                    break
        return out

    def counts(self) -> dict[str, int]:
        pending = sum(1 for e in self.entries.values()
                      if e.status == _PENDING)
        visited = sum(1 for e in self.entries.values()
                      if e.status == _VISITED)
        return {"pending": pending, "visited": visited,
                "total": len(self.entries)}

    def to_record(self) -> dict:
        """Compact summary for steps.jsonl / wire records."""
        c = self.counts()
        recent = [e.label for e in
                  sorted(self.entries.values(),
                         key=lambda e: e.last_seen_step, reverse=True)
                  if e.status == _VISITED and e.label][:8]
        dead = [{"label": (k.split("|")[-1] if "|" in k else k),
                 "count": v.get("count", 0),
                 "kinds": v.get("kinds", [])}
                for k, v in sorted(
                    self.dead.items(),
                    key=lambda kv: kv[1].get("last_step", 0),
                    reverse=True)][:8]
        return {"pending": c["pending"], "visited": c["visited"],
                "pending_labels": self.pending_labels(),
                "recent_visited": recent, "dead_actions": dead}

    # -- persistence ---------------------------------------------------------

    def take_events(self) -> list[dict]:
        """Drain queued ledger events for the JSONL writer."""
        out = self._pending_events
        self._pending_events = []
        return out

    def snapshot(self) -> dict:
        """Full serializable state (frontier.json + mission resume)."""
        return {"entries": [asdict(self.entries[k]) for k in self.order],
                "dead": dict(self.dead)}

    @classmethod
    def from_snapshot(cls, data: dict | None) -> "Frontier":
        """Rebuild from a snapshot dict. Tolerates garbage (returns empty)."""
        fr = cls()
        try:
            for key, rec in ((data or {}).get("dead") or {}).items():
                if not isinstance(rec, dict):
                    continue
                try:
                    count = int(rec.get("count", 0) or 0)
                except (ValueError, TypeError):
                    continue
                if count > 0 and isinstance(key, str):
                    fr.dead[key] = {
                        "count": count,
                        "kinds": list(rec.get("kinds") or []),
                        "last_step": int(rec.get("last_step", 0) or 0),
                    }
        except Exception:
            pass
        try:
            for raw in (data or {}).get("entries", []):
                if not isinstance(raw, dict):
                    continue
                ent = FrontierEntry(
                    key=str(raw.get("key") or ""),
                    url=str(raw.get("url") or ""),
                    label=str(raw.get("label") or ""),
                    host=str(raw.get("host") or ""),
                    status=str(raw.get("status") or _PENDING),
                    first_seen_step=raw.get("first_seen_step") or 0,
                    last_seen_step=raw.get("last_seen_step") or 0,
                    visits=raw.get("visits") or 0,
                    source=str(raw.get("source") or "replay"),
                    title=str(raw.get("title") or ""))
                # Coerce defensively; a corrupt line must not kill resume.
                ent.status = ent.status if ent.status in (
                    _PENDING, _VISITED) else _PENDING
                try:
                    ent.visits = int(ent.visits or 0)
                    ent.first_seen_step = int(ent.first_seen_step or 0)
                    ent.last_seen_step = int(ent.last_seen_step or 0)
                except (ValueError, TypeError):
                    ent.visits, ent.first_seen_step, \
                        ent.last_seen_step = 0, 0, 0
                if ent.key and ent.key not in fr.entries:
                    fr.entries[ent.key] = ent
                    fr.order.append(ent.key)
        except Exception:
            return cls()
        return fr

    @staticmethod
    def load_events_file(path: str) -> "Frontier":
        """Rebuild by replaying a frontier.jsonl event ledger."""
        fr = Frontier()
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    kind = ev.get("event")
                    step = ev.get("step", 0)
                    if kind == "discovered":
                        key = ev.get("key", "")
                        if key and key not in fr.entries:
                            fr.entries[key] = FrontierEntry(
                                key=key, url=ev.get("url", ""),
                                label=ev.get("label", ""),
                                host="", status=_PENDING,
                                first_seen_step=step,
                                last_seen_step=step, source="replay")
                            fr.order.append(key)
                    elif kind in ("visited", "aliased"):
                        fr.mark_visited(ev.get("url", ""),
                                        label=ev.get("label", ""),
                                        step=step)
        except OSError:
            pass
        fr._pending_events = []
        return fr


def load_frontier_file(path: str) -> Frontier:
    """Load frontier.json snapshot; empty frontier on any failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return Frontier.from_snapshot(json.load(f))
    except (OSError, ValueError, AttributeError):
        return Frontier()
