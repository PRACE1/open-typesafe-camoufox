---
title: "Ref Type Dictionary (aria identity contract)"
tags: [refs, aria, resolution, agent-contract]
status: active
created: 2026-09-18
---

# Ref type dictionary — what the driving agent may hold, and how each resolves

Source taxonomy: `overtimepog/camoufox-mcp` (`camoufoxmcp/snapshot.py`,
`camoufoxmcp/server.py`). Every entry below maps to its OTC implementation
(file + symbol). Refs are session-ephemeral: a ref is valid from the
snapshot that minted it until the next navigation. Never invent a ref;
never reuse one across page loads.

Decision priority (model choices AND resolution): **aria ref → durable
selector → positional → raw CSS (synthesis only) → eval hatch**. The first
ok verdict wins; stale only when no tier resolves ok; the coordinate
point-check stays the final misclick guard either way.

Role categories across the dictionary: `interactive`, `landmark`,
`content`, `frame`, `css_fallback`, `eval_escape_hatch`.

## 1. Native interactive refs (the only handles Jev may choose)

| ref_type | pattern | example | OTC source | resolution |
|---|---|---|---|---|
| `aria_interactive_ref` | `^@?e\d+$` | `@e1` | `src/capability/aria_refs.py::parse_aria_snapshot`, `src/perception.py::_elements_from_aria` | known map → `aria-ref=eN` (`src/capability/aria_refs.py::resolve_ref`) |

Roles that mint actionable refs: `link`, `button`, `textbox`, `combobox`,
`searchbox`, `spinbutton`, `checkbox`, `switch`, `radio`, `menuitem`,
`menuitemcheckbox`, `menuitemradio`, `tab`, `slider`
(`ROLE_TO_KIND` maps them to harness kinds). Framed variants serialize as
`fNeM` and resolve at page level — Playwright routes frames natively
(verified live, incl. recaptcha iframes).

## 2. Structural landmarks (context, never actionable)

Kept through compaction so modals/dialogs are never trimmed blind.
Parsed by ancestor walk in `parse_aria_snapshot`; nearest landmark becomes
the element's `region`. Our set matches the taxonomy exactly:

| ref_type | pattern | example | OTC region value |
|---|---|---|---|
| `aria_structural_landmark_main` | `^-\s*main` | `- main:` | `main` |
| `aria_structural_landmark_navigation` | `^-\s*navigation` | `- navigation:` | `navigation` |
| `aria_structural_landmark_contentinfo` | `^-\s*contentinfo` | `- contentinfo:` | `contentinfo` |
| `aria_structural_landmark_banner` | `^-\s*banner` | `- banner:` | `banner` |
| `aria_structural_landmark_alert` | `^-\s*alert` | `- alert:` | `alert` |
| `aria_structural_landmark_dialog` | `^-\s*dialog` | `- dialog:` | `dialog` |
| `aria_structural_landmark_alertdialog` | `^-\s*alertdialog` | `- alertdialog:` | `alertdialog` |
| `aria_structural_landmark_complementary` | `^-\s*complementary` | `- complementary:` | `complementary` |
| `aria_structural_landmark_search` | `^-\s*search` | `- search:` | `search` |
| `aria_structural_landmark_form` | `^-\s*form` | `- form:` | `form` |
| `aria_structural_landmark_region` | `^-\s*region` | `- region:` | `region` |
| `aria_structural_landmark_log` | `^-\s*log` | `- log:` | `log` |
| `aria_structural_landmark_status` | `^-\s*status` | `- status:` | `status` |

`STRUCTURAL_ROLES` (`aria_refs.py`) is the single list; a blocked-page
detector MUST NOT treat `dialog`/`alertdialog` containers as stop signals
(see `EDGE_CASES.md` #6/#24 — bot-check pages are worked, not fled).

## 3. Content refs (parsed, attached, never chosen)

| ref_type | pattern | example | OTC handling |
|---|---|---|---|
| `aria_content_ref` | `^\[ref=(e\d+)\]` | `paragraph [ref=e4]: inline text` | headings/paragraphs/text runs parse into `AriaNode` but `interactive_nodes()` drops them; `/url:` and `text:` child lines attach to the parent interactive node |

## 4. Frame handles

| ref_type | pattern | example | OTC resolution |
|---|---|---|---|
| `frame_index_ref` | `^f\d+$` | `f0` | `resolve_ref` → `iframe:nth-of-type(N+1)` (`frame_aware: true`) |

Jev never emits bare `fN` (options only carry full refs); the branch exists
for toolbox completeness and is covered in `tests/test_aria_refs.py`.

## 5. CSS fallbacks (synthesis only — Jev never emits these)

| ref_type | pattern | example | OTC resolution |
|---|---|---|---|
| `css_id_selector` | `.*#.*` | `#submit-btn` | raw passthrough to `page.locator` |
| `css_class_selector` | `.*\..*` | `.primary-btn` | raw passthrough |
| `css_attribute_selector` | `.*\[.*\].*` | `input[name="email"]` | raw passthrough |
| `css_tag_selector` | `^[a-zA-Z].*` | `button` | raw passthrough |
| `css_universal_selector` | `^\*.*` | `*[data-action="dismiss"]` | raw passthrough |

Detection order in `resolve_ref` is known-ref → `fN` → CSS → last-resort
`aria-ref=`. **Caution:** an unknown `e99`-shaped string hits the CSS-tag
branch (empty match → safe refusal downstream). CSS selectors reach the
page ONLY through validator-gated synthesized capabilities
(`capability/validator.py`); the runner has no direct eval/CSS kind.

## 6. Eval escape hatch

| ref_type | pattern | example | OTC resolution |
|---|---|---|---|
| `js_expression_eval` | `.*` | `document.querySelector('.popover-dismiss').click()` | `page.evaluate(expression)` inside a synthesized `execute()`, after AST + signature + 3s dry-run gates |

## 7. OTC-only extensions (not in the taxonomy)

| handle | minted where | resolution |
|---|---|---|
| positional `eN` refs | DOM-probe fallback (`element_probe.py`, `perception.py::_elements_from_probe`) when snapshots fail | nth-match + strict cross-check (`resolve.py::_resolve_target`) |
| durable selectors (`#id`, `tag[name=]`, `tag[aria-label=]`, structural path) | `ELEMENT_PROBE_JS::genSel` (read-only, uniqueness-checked) | `_resolve_by_selector`, first stop in heal remap after aria |
| `: value` / `[checked]` / `[active]` flags | snapshot suffixes (`aria_refs.py::_split_value`) | fill-state (`value_len`), retype-submit guard, live `read_input_value` backup |

## Agent rules (normative)

1. Choose only refs from the current map; prefer aria refs, accept positional.
2. A ref that fails to resolve is STALE, not broken tooling — heal-remap by
   aria → selector → label, exactly once, then account.
3. Framed refs (`fNeM`) work as-is; never split the frame prefix off.
4. Raw CSS and eval exist ONLY inside validated syntheses, never in votes.
5. `dialog`/`alertdialog` in state means work-the-controls (challenge kind),
   image puzzles mean honest stop — never flee a bot-check page.
