"""aria_refs.py — accessibility-snapshot identity layer (playwright-cli contract).

Camoufox delegates selector resolution to Playwright; there is no selector
engine inside Camoufox itself. This module ports the overtimepog/camoufox-mcp
approach (camoufoxmcp/snapshot.py): parse `page.aria_snapshot(mode="ai")`
YAML into records with ephemeral refs, and resolve actions through the
native `aria-ref=eN` selector — zero DOM writes, zero bot footprint.

Refs are session-ephemeral (die on navigation); framed nodes serialize as
fNeM and resolve at page level. Anything outside the snapshot (portals,
unmounted popovers) falls through the 3-tier cascade in resolve_ref().
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REF_RE = re.compile(r"\[ref=((?:f\d+)?e\d+)\]")
QUOTED_RE = re.compile(r'"([^"]*)"|\'([^\']*)\'')
FLAG_RE = re.compile(r"\[([^\]]+)\]")
URL_PREFIX = "/url:"

ROLE_TO_KIND = {
    "link": "link",
    "button": "button",
    "textbox": "input",
    "combobox": "input",
    "searchbox": "input",
    "spinbutton": "input",
    "checkbox": "checkbox",
    "switch": "checkbox",
    "radio": "radio",
    "menuitem": "button",
    "menuitemcheckbox": "checkbox",
    "menuitemradio": "radio",
    "tab": "button",
    "slider": "slider",
}

INTERACTIVE_ROLES = frozenset(ROLE_TO_KIND)

STRUCTURAL_ROLES = frozenset({
    "main", "navigation", "contentinfo", "banner", "alert", "dialog",
    "alertdialog", "complementary", "search", "form", "region", "log",
    "status",
})


@dataclass
class AriaNode:
    """One parsed snapshot line with an identity worth keeping."""

    ref: str = ""
    role: str = ""
    name: str = ""
    url: str = ""
    value: str = ""
    flags: tuple[str, ...] = ()
    depth: int = 0
    region: str = ""


def _strip_ref(line: str) -> tuple[str, str]:
    """Remove the ref token; return (cleaned, ref)."""
    m = REF_RE.search(line)
    ref = m.group(1) if m else ""
    return REF_RE.sub("", line).strip(), ref


def _first_name(body: str) -> str:
    """First quoted span (double or single quotes) as the accessible name."""
    m = QUOTED_RE.search(body)
    if not m:
        return ""
    return m.group(1) if m.group(1) is not None else m.group(2)


def _split_value(body: str) -> tuple[str, str]:
    """Split a trailing `: value` suffix (textbox values render this way).

    Quoted names are removed first so colons inside names never split.
    """
    dequoted = QUOTED_RE.sub("", body).strip()
    m = re.search(r":\s*(.*?)\s*$", dequoted)
    if not m:
        return body, ""
    value = m.group(1)
    head = body.rstrip()
    if not value:
        return (head[:-1].rstrip() if head.endswith(":") else head), ""
    idx = head.rfind(value)
    if idx >= 0:
        head = head[:idx].rstrip()
    if head.endswith(":"):
        head = head[:-1].rstrip()
    return head, value


def parse_aria_snapshot(text: str) -> list[AriaNode]:
    """Parse an aria snapshot into nodes (interactive or structural).

    Static text and url-child lines attach to their parent; everything
    else is dropped. Never raises on malformed input.
    """
    nodes: list[AriaNode] = []
    stack: list[AriaNode] = []  # open ancestors by depth
    pending_url = ""
    for raw in (text or "").splitlines():
        if not raw.strip() or raw.strip() == "-":
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        body = raw.strip()
        if body.startswith("- "):
            body = body[2:].strip()
        depth = indent // 2
        while stack and stack[-1].depth >= depth:
            stack.pop()
        if body.startswith(URL_PREFIX):
            pending_url = body[len(URL_PREFIX):].strip()
            if stack and not stack[-1].url:
                stack[-1].url = pending_url
            continue
        if body.startswith("text:"):
            continue
        noref, ref = _strip_ref(body)
        if not ref:
            continue
        role = noref.split()[0] if noref.split() else ""
        if not role:
            continue
        name = _first_name(noref)
        flags = tuple(f for f in FLAG_RE.findall(noref) if f)
        rest, value = _split_value(FLAG_RE.sub("", noref).strip())
        _ = rest
        region = ""
        for anc in reversed(stack):
            if anc.role in STRUCTURAL_ROLES:
                region = anc.role
                break
        node = AriaNode(ref=ref, role=role, name=name.strip(),
                        value=value.strip(), flags=flags, depth=depth,
                        region=region)
        nodes.append(node)
        stack.append(node)
    return nodes


def interactive_nodes(nodes: list[AriaNode]) -> list[AriaNode]:
    """Nodes Jev may act on: interactive roles only, landmarks excluded."""
    return [n for n in nodes if n.role in INTERACTIVE_ROLES]


def resolve_ref(ref: str, known_refs: set[str] | None = None) -> str:
    """3-tier selector cascade (camoufox-mcp resolve_ref mechanics).

    1. Known snapshot ref -> native `aria-ref=eN` (framed fNeM included).
    2. Bare frame handle `fN` -> `iframe:nth-of-type(N+1)`.
    3. Raw CSS passthrough (contains #, ., [, or leads with alpha/*).
    Last resort: `aria-ref=` prefix and let Playwright report empty.
    """
    clean = (ref or "").lstrip("@")
    known = known_refs or set()
    if clean in known:
        return f"aria-ref={clean}"
    if re.fullmatch(r"f\d+", clean):
        try:
            return f"iframe:nth-of-type({int(clean[1:]) + 1})"
        except ValueError:
            pass
    if any(c in clean for c in "#.[]") or clean[:1].isalpha() or clean.startswith("*"):
        return clean
    return f"aria-ref={clean}"
