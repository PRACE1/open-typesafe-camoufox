"""Perception tests — credential detection, formatting, state packet (offline)."""

from src.deps import ElementRef, FocusedField
from src.perception import (
    _unwrap_redirect, build_state, element_criteria, excerpt_for,
    format_elements, host_of, is_blocked_page, is_credential,
    is_credential_element, norm_url, page_fingerprint, trim_notes,
)


def test_is_credential_password_type():
    assert is_credential("input", "password", "", "") is True


def test_is_credential_label_hint():
    assert is_credential("input", "text", "Password", "") is True
    assert is_credential("input", "text", "", "Enter passcode") is True
    assert is_credential("input", "text", "Username", "") is False


def test_is_credential_autocomplete():
    assert is_credential("input", "text", "", "", autocomplete="current-password") is True
    assert is_credential("input", "text", "Search", "", autocomplete="off") is False


def test_is_credential_plain_search_false():
    assert is_credential("textarea", "", "Search", "Search Google") is False


def test_is_credential_element():
    assert is_credential_element(ElementRef(idx=0, kind="input", type="password")) is True
    assert is_credential_element(ElementRef(idx=1, kind="input", label="Search")) is False


def test_format_elements_empty():
    assert "no actionable" in format_elements([])


def test_format_elements_line():
    out = format_elements([ElementRef(idx=3, kind="input", type="text", label="Search", cx=0.5, cy=0.4)])
    assert out == '[3] input (text) "Search" @ 0.500,0.400'


def test_element_criteria():
    c = element_criteria(ElementRef(idx=0, kind="link", text="Moon", cx=0.1, cy=0.2))
    assert c == 'link "Moon" at 0.100,0.200'


def test_element_criteria_fill_state():
    c = element_criteria(ElementRef(idx=5, kind="textarea", label="Search", cx=0.5, cy=0.4))
    assert c.endswith("empty")
    c2 = element_criteria(ElementRef(idx=5, kind="textarea", label="Search",
                                     cx=0.5, cy=0.4, value="hello", value_len=5))
    assert c2.endswith("filled(5ch)")
    c3 = element_criteria(ElementRef(idx=1, kind="link", text="Moon", cx=0.1, cy=0.2))
    assert "filled" not in c3 and "empty" not in c3


def test_find_elements_parses_values():
    import asyncio

    from src.perception import find_elements

    class _FakePage:
        async def evaluate(self, js):
            return [{"idx": 0, "kind": "input", "type": "text", "id": "",
                     "label": "Search", "placeholder": "", "text": "",
                     "value": "typed query", "value_len": 11,
                     "cx": 0.5, "cy": 0.4},
                    {"idx": 1, "kind": "link", "text": "Result"}]

    els = asyncio.run(find_elements(type("P", (), {"page": _FakePage()})()))
    assert els[0].value == "typed query" and els[0].value_len == 11
    assert els[1].value == "" and els[1].value_len == 0


def test_build_state_shape():
    els = [ElementRef(idx=0, kind="input", label="Search")]
    s = build_state(task="t", url="https://a.example", elements=els,
                    focused=FocusedField(role="input"), page_text="hello",
                    history=["a", "b"], frame="f", grid="80x20")
    assert s["task"] == "t" and s["url"] == "https://a.example"
    assert len(s["elements"]) == 1 and s["elements"][0]["label"] == "Search"
    assert s["focused_field"]["role"] == "input"
    assert s["page_text"] == "hello" and s["history"] == ["a", "b"]
    assert s["tabs"] == 1
    assert s["elements"][0]["sel"] == '[data-jev="0"]'


def test_build_state_tabs_param():
    s = build_state(task="t", url="u", elements=[], focused=FocusedField(),
                    page_text="", history=[], tabs=3)
    assert s["tabs"] == 3


def test_norm_url_drops_tracking_keeps_query():
    a = norm_url("https://www.google.com/search?q=moon&sca_esv=XYZ&ved=123#frag")
    b = norm_url("https://www.google.com/search?ved=456&q=moon&sca_esv=ABC")
    assert a == b == "https://www.google.com/search?q=moon"
    assert norm_url("https://EXAMPLE.com/Path") == "https://example.com/Path"
    assert norm_url("not a url") == "not a url"


def test_page_fingerprint_equal_and_differ():
    assert page_fingerprint("https://a.example/x", "hello world") == \
        page_fingerprint("https://a.example/x?utm_source=t", "hello world")
    assert page_fingerprint("https://a.example/x", "hello") != \
        page_fingerprint("https://a.example/x", "goodbye")
    assert page_fingerprint("https://a.example/x", "t") != \
        page_fingerprint("https://a.example/y", "t")


def test_trim_notes_drops_oldest():
    notes = ["a" * 900, "b" * 900, "c" * 900]
    trimmed = trim_notes(notes, limit=2000)
    assert trimmed == ["c" * 900] or sum(len(n) for n in trimmed) <= 2000
    assert trimmed[-1] == "c" * 900
    assert trim_notes(["x"], limit=2000) == ["x"]


def test_element_criteria_visited_marker():
    from src.perception import norm_url as _n
    e = ElementRef(idx=2, kind="link", text="Result", cx=0.3, cy=0.4,
                   href="https://a.example/page?utm_source=x")
    assert element_criteria(e).endswith("(visited)") is False
    assert element_criteria(e, {_n("https://a.example/page")}).endswith("(visited)")
    assert element_criteria(e, {"https://other.example/"}).endswith("(visited)") is False


def test_build_state_notes_visited_and_href():
    els = [ElementRef(idx=0, kind="link", text="R", href="https://a.example/p")]
    s = build_state(task="t", url="u", elements=els, focused=FocusedField(),
                    page_text="", history=[],
                    notes=["n1", "n2"], visited=["https://a.example/p"],
                    lessons="popups die with openers")
    assert s["notes"] == ["n1", "n2"] and s["visited"] == ["https://a.example/p"]
    assert s["elements"][0]["href"] == "https://a.example/p"
    assert s["lessons"] == "popups die with openers"


def test_find_elements_resolves_href():
    import asyncio

    from src.perception import find_elements

    class _FakePage:
        url = "https://base.example/dir/page"
        async def evaluate(self, js):
            return [{"idx": 0, "kind": "link", "text": "R",
                     "href": "/other?q=1", "cx": 0.1, "cy": 0.1}]

    els = asyncio.run(find_elements(type("P", (), {"page": _FakePage()})()))
    assert els[0].href == "https://base.example/other?q=1"


def test_unwrap_redirect_only_url_path():
    assert _unwrap_redirect("https://www.google.com/url?q=https://dest.example/a&sa=t") == \
        "https://dest.example/a"
    assert _unwrap_redirect("https://www.google.com/search?q=moon") == \
        "https://www.google.com/search?q=moon"
    assert _unwrap_redirect("https://www.google.com/goto?url=OPAQUE") == \
        "https://www.google.com/goto?url=OPAQUE"
    assert host_of("https://Dest.Example/a") == "dest.example"
    assert host_of("not a url") == ""


def test_criteria_region_and_host_markers():
    e = ElementRef(idx=3, kind="link", text="Prolific", cx=0.3, cy=0.4,
                   href="https://www.prolific.com/", region="main")
    c = element_criteria(e)
    assert "[main]" in c and "-> www.prolific.com" in c
    e2 = ElementRef(idx=0, kind="link", text="X", cx=0.1, cy=0.1)
    c2 = element_criteria(e2)
    assert "[" not in c2 and "->" not in c2


def test_find_elements_parses_region():
    import asyncio

    from src.perception import MAX_ELEMENTS, find_elements

    assert MAX_ELEMENTS == 60

    class _FakePage:
        url = "https://base.example/"
        async def evaluate(self, js):
            return [{"idx": 0, "kind": "link", "text": "R", "region": "main",
                     "href": "https://a.example/", "cx": 0.1, "cy": 0.1}]

    els = asyncio.run(find_elements(type("P", (), {"page": _FakePage()})()))
    assert els[0].region == "main"


def test_excerpt_for_anchors_on_task_keywords():
    task = "Search for Europa water ocean facts"
    text = "Get app Write Sign up " + ("filler words here. " * 40) + \
        "Europa hides a salty water ocean beneath ice. " + ("more filler. " * 40)
    out = excerpt_for(task, text)
    assert len(out) == 300 and "salty water ocean" in out
    assert excerpt_for(task, "short text") == "short text"
    assert excerpt_for(task, "") == ""
    chrome = "Get app Write Sign up " * 30
    assert excerpt_for("unrelated zzzqqq", chrome) == chrome[:300]


def test_excerpt_prefers_dense_body_over_chrome_head():
    # Regression: generic words ("search") hit nav chrome first; the dense
    # article window must win anyway.
    task = "Search English Wikipedia for Europa moon water facts"
    chrome = "Jump to content Main menu Search Donate Create account Log in "
    body = ("Europa is an icy moon of Jupiter. " * 8
            + "Its subsurface water ocean may hold twice the water of Earth. "
            + ("orbital resonance details. " * 20))
    text = chrome + body
    out = excerpt_for(task, text)
    assert "subsurface water ocean" in out
    assert not out.startswith("Jump to content")


def test_is_blocked_page_markers():
    sorry = "https://www.google.com/sorry/index?continue=https://www.google.com/search&q=x"
    assert is_blocked_page(sorry, "anything") == "bot-check-by-url:sorry"
    assert is_blocked_page("https://www.google.com/", "Our systems have detected unusual traffic from your network") == \
        "bot-check-by-text:unusual traffic"
    assert is_blocked_page("https://a.example/", "Please complete the captcha below") == \
        "bot-check-by-text:captcha"
    assert is_blocked_page("https://a.example/", "Verify you are human to continue") == \
        "bot-check-by-text:verify you are human"
    assert is_blocked_page("https://a.example/", "Access Denied - request blocked") == \
        "bot-check-by-text:access denied"
    assert is_blocked_page("https://a.example/", "The Moon has water ice.") is None
    assert is_blocked_page("https://a.example/", "   ") is None
    assert is_blocked_page("https://a.example/", "") is None


def test_build_state_page_state_blocked_and_normal():
    s = build_state(task="t", url="u", elements=[], focused=FocusedField(),
                    page_text="", history=[])
    assert s["page_state"] == "normal"
    s2 = build_state(task="t", url="u", elements=[], focused=FocusedField(),
                     page_text="", history=[], blocked="bot-check-by-url:sorry")
    assert s2["page_state"].startswith("blocked:bot-check-by-url:sorry")
    assert "like any page" in s2["page_state"]
