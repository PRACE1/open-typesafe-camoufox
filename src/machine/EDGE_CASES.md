# Edge-case catalog — every way a run can go sideways, and what handles it

Each row: the edge, its detection signal, the machine state(s) involved,
the guard/action that resolves it, and current status. Numbers are stable
IDs used in commit messages and the run log (`EDGE #n`).

| # | Edge | Detection signal | State / guard / action | Status |
|---|---|---|---|---|
| 1 | Blank / transitioning page | 0 elements, empty text | `seeing` → `deciding` → `wait`; `page_ready` Noul low | handled (`runner.py`, `decide.py`) |
| 2 | Loading spinner / "Looking for results" | `page_settled()` false | `wait` (neutral, never counts to stops) | handled |
| 3 | Language banner mistaken for loading | text ≥300ch with banner | `page_settled()` length rule | handled |
| 4 | Covered click point (overlay span) | `elementFromPoint` ≠ target | `acting`: skip with `covered:<tag>` diagnosis; anchors/buttons click through | handled (`actions.py`) |
| 5 | Stale idx after re-render | live kind ≠ decided kind | `acting`: `stale map` abort, no dispatch | handled |
| 6 | **Bot-check / CAPTCHA page** (Google `/sorry`, "unusual traffic", recaptcha text) | `is_blocked_page()` on URL+text | `seeing` records `blocked` context; loop keeps classifying positions + moving the cursor humanized, iteratively until it resolves (bounded by budget/noop rails) | handled (`perception.py`, `runner.py`, state `page_state`) |
| 7 | Click opens new tab | tab-set diff after click / at SEE | `acting`→adopt; `seeing` reconciles slow popups; opener kept on close | handled |
| 8 | Popup dies with opener | live probe (`ME_CLOSED`) | `close_other_tabs` keeps current+opener; survivor fallback | handled |
| 9 | Stale tab listeners fire | event source ≠ bound page | generation guard in all three nav handlers | handled (`cursor_tracking.py`) |
| 10 | Bare input click fixation | target kind in (input, textarea, select) | veto → proposal fallback → neutral idle | handled |
| 11 | Kind/item mismatch | `fits` Noul < 0.4 | neutral idle (never executes known-dead) | handled |
| 12 | Repetition (3× same kind+item+url) | `loop_guard_trip` | forced wait + no-op count | handled |
| 13 | Dead action (no observable effect) | fingerprint equal, consecutive | stop `action had no observable effect twice` | handled |
| 14 | Credential field | type=password / autocomplete / label hints | writer `fill:false`; `{ENV}` placeholders only | handled |
| 15 | Low confidence (< 0.4) | kind Choice confidence | gate idle + no-op count | handled |
| 16 | Slow navigation (SEE races landing) | fingerprint equal, nav in flight | `settle_after_action` poll (newtab/navigated/same) | handled |
| 17 | JS dialog blocks input | `dialog` event | auto accept/dismiss on the BOUND tab only | handled |
| 18 | Empty element map | probe returns [] | sentinel item option; `wait` | handled |
| 19 | Decide/model exception | raised in `decide_action`/proposer | `gating`→`verifying` idle path; fail-soft counters | handled |
| 20 | Budget / max-steps exhausted | loop condition | `stopped`, reason recorded | handled |
| 21 | No API keys | empty key at call time | deterministic offline fallbacks (kind `none`/writer decline) | handled |
| 22 | Login wall (account required) | task needs auth, no session | NOT handled — planner-visible; requires human credentials (out of scope by design) | open |

## Notes on #6 (blocked pages)

Detection is deterministic (`perception.is_blocked_page`): Google `/sorry/`
path, "unusual traffic" text, recaptcha/verify-human markers, access-denied
markers. The reason rides in the state packet (`page_state`) so Jev
understands the context, and the loop works the page's verification controls
(checkbox/button/input — the probe covers all inputs) with the same
classify → humanized move → verify cycle as any page, bounded by the normal
budget/no-op/dead-run rails. There is no special stop and no special
solver: a bot-check page is just a page.
