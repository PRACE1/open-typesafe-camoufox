"""aria_refs tests — snapshot parsing + 3-tier cascade (offline fixtures).

Fixtures are verbatim lines captured live from Camoufox
page.aria_snapshot(mode="ai") on google.com (2026-09-17).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.capability.aria_refs import (
    ROLE_TO_KIND,
    interactive_nodes,
    parse_aria_snapshot,
    resolve_ref,
)

HOME_FIXTURE = """\
- generic [ref=e2]:
  - navigation [ref=e3]:
    - generic [ref=e5]:
      - generic [ref=e6]:
        - link "Gmail" [ref=e8] [cursor=pointer]:
          - /url: https://mail.google.com/mail/&ogbl
        - link "Images" [ref=e10] [cursor=pointer]:
          - /url: https://www.google.com/imghp?hl=en&ogbl
          - text: Images
      - button "Google apps" [ref=e13] [cursor=pointer]
      - link "Sign in" [ref=e18] [cursor=pointer]:
        - /url: https://accounts.google.com/ServiceLogin?hl=en
  - img "Google" [ref=e22]
  - search [ref=e30]:
    - generic [ref=e32]:
      - combobox "Search" [active] [ref=e44]: Superteam Earn
      - generic [ref=e76]:
        - button "Google Search" [ref=e77] [cursor=pointer]
  - generic [ref=e81]:
    - text: "Google offered in:"
    - link "English" [ref=e83] [cursor=pointer]:
"""

SORRY_FIXTURE = """\
- generic [ref=f2e2]:
  - separator [ref=f2e3]
  - iframe [ref=f2e8]:
    - generic [ref=f3e2]:
      - generic [ref=f3e3]:
        - checkbox "I'm not a robot" [ref=f3e7]
        - generic [ref=f3e9]: I'm not a robot
    - text: About this page Our systems have detected unusual traffic
    - link "Why did this happen?" [ref=f2e11] [cursor=pointer]:
      - /url: "#"
"""


def _by_ref(nodes):
    return {n.ref: n for n in nodes}


def test_parse_home_links_buttons_combobox():
    nodes = _by_ref(parse_aria_snapshot(HOME_FIXTURE))
    gmail = nodes["e8"]
    assert (gmail.role, gmail.name) == ("link", "Gmail")
    assert gmail.url == "https://mail.google.com/mail/&ogbl"
    assert gmail.region == "navigation"
    assert "cursor=pointer" in gmail.flags
    combo = nodes["e44"]
    assert combo.role == "combobox" and combo.value == "Superteam Earn"
    assert "active" in combo.flags
    assert nodes["e22"].role == "img"
    assert nodes["e3"].role == "navigation" and nodes["e30"].role == "search"
    # static text + url children attach, never become nodes
    assert "text:" not in [n.role for n in nodes.values()]
    assert all(n.ref for n in nodes.values())


def test_parse_framed_refs_and_checkbox():
    nodes = _by_ref(parse_aria_snapshot(SORRY_FIXTURE))
    box = nodes["f3e7"]
    assert (box.role, box.name) == ("checkbox", "I'm not a robot")
    assert nodes["f2e11"].url == '"#"' or nodes["f2e11"].url == "#"
    assert nodes["f2e8"].role == "iframe"


def test_interactive_filter_and_role_map():
    nodes = parse_aria_snapshot(HOME_FIXTURE + SORRY_FIXTURE)
    inter = interactive_nodes(nodes)
    refs = {n.ref for n in inter}
    assert {"e8", "e13", "e44", "e77", "f3e7", "f2e11"} <= refs
    assert "e22" not in refs and "e3" not in refs and "e81" not in refs
    assert ROLE_TO_KIND["combobox"] == "input"
    assert ROLE_TO_KIND["checkbox"] == "checkbox"


def test_parse_tolerates_garbage():
    assert parse_aria_snapshot("") == []
    assert parse_aria_snapshot("-\n   \nnot a snapshot line\n") == []
    nodes = parse_aria_snapshot('- button [ref=e1]\n- "quoted" [ref=e2]\n')
    assert [(n.role, n.name) for n in nodes] == [("button", ""), ('"quoted"', "quoted")]
    # url child without a parent stack is dropped safely
    assert parse_aria_snapshot("- /url: https://x.example/\n") == []


def test_resolve_ref_cascade():
    known = {"e5", "f3e7"}
    assert resolve_ref("e5", known) == "aria-ref=e5"
    assert resolve_ref("@e5", known) == "aria-ref=e5"
    assert resolve_ref("f3e7", known) == "aria-ref=f3e7"
    assert resolve_ref("f0", known) == "iframe:nth-of-type(1)"
    assert resolve_ref("f2", known) == "iframe:nth-of-type(3)"
    assert resolve_ref("#submit-btn", known) == "#submit-btn"
    assert resolve_ref("div[role=dialog] button.close", known) == \
        'div[role=dialog] button.close'
    assert resolve_ref("e99", known) == "e99"  # unknown e-pattern: CSS tag, empty match
    assert resolve_ref("9lives", known) == "aria-ref=9lives"  # last resort


def test_ref_types_doc_covers_taxonomy():
    """docs/REF_TYPES.md maps every taxonomy category to our implementation,
    so the driving agent loads ref semantics with the session."""
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    doc = open(os.path.join(root, "docs", "REF_TYPES.md"),
               encoding="utf-8").read()
    for category in ("interactive", "landmark", "content", "frame",
                     "css_fallback", "eval_escape_hatch"):
        assert category in doc, category
    for ref_type in ("aria_interactive_ref", "aria_content_ref",
                     "frame_index_ref", "css_id_selector",
                     "css_class_selector", "css_attribute_selector",
                     "css_tag_selector", "css_universal_selector",
                     "js_expression_eval"):
        assert ref_type in doc, ref_type
    for landmark in ("contentinfo", "banner", "alertdialog", "complementary"):
        assert f"aria_structural_landmark_{landmark}" in doc, landmark
    assert "src/capability/aria_refs.py" in doc
