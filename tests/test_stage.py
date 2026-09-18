"""Stage classifier tests — pure metadata in, stage out (offline)."""

from src.capability.stage import (
    CHECKBOX,
    IMAGE_GRID,
    INVISIBLE,
    MANAGED,
    MOTION,
    NONE,
    SLIDER,
    TEXT,
    TURNSTILE,
    UNKNOWN,
    classify_stage,
    provider_from_url,
)


def test_checkbox_anchor_frame():
    res = classify_stage({
        "url": "https://www.google.com/recaptcha/api2/demo", "text": "form",
        "frames": [{"url": "https://www.google.com/recaptcha/api2/anchor"
                           "?k=KEY", "title": "reCAPTCHA", "text": "",
                    "visible": True}]})
    assert res.stage == CHECKBOX and res.provider == "recaptcha"
    assert res.auto_solvable and not res.needs_vision


def test_hidden_frame_is_not_a_stage():
    res = classify_stage({
        "url": "https://example.test/", "text": "ordinary article",
        "frames": [{"url": "https://www.google.com/recaptcha/api2/bframe",
                    "visible": False}]})
    assert res.stage == NONE


def test_bframe_url_means_grid():
    res = classify_stage({
        "url": "https://example.test/", "text": "",
        "frames": [{"url": "https://www.google.com/recaptcha/api2/bframe",
                    "visible": True}]})
    assert res.stage == IMAGE_GRID and res.needs_vision


def test_instruction_copy_beats_generic_url():
    res = classify_stage({
        "url": "https://example.test/", "text": "",
        "frames": [{"url": "https://example.test/widget",
                    "text": "Select all images with bicycles",
                    "visible": True}]})
    assert res.stage == IMAGE_GRID


def test_host_marketing_copy_loses_to_child_frame():
    res = classify_stage({
        "url": "https://example.test/", "title": "slider captcha demo",
        "text": "drag the slider to verify",
        "frames": [{"url": "https://www.google.com/recaptcha/api2/anchor",
                    "title": "reCAPTCHA", "visible": True}]})
    assert res.stage == CHECKBOX


def test_turnstile_widget():
    res = classify_stage({
        "url": "https://example.test/", "text": "",
        "frames": [{"url": "https://challenges.cloudflare.com/turnstile/v0/x",
                    "visible": True}]})
    assert res.stage == TURNSTILE and res.provider == "turnstile"


def test_managed_cloudflare_wall():
    res = classify_stage({"url": "https://example.test/",
                          "text": "Just a moment ... checking your browser"})
    assert res.stage == MANAGED and res.auto_solvable


def test_motion_slider_text():
    assert classify_stage(
        {"text": "click on the shape that grows"})["stage"] == MOTION
    assert classify_stage(
        {"text": "drag the slider to complete the puzzle",
         "frames": []})["stage"] == SLIDER
    assert classify_stage(
        {"text": "type the characters you see to verify captcha",
         "frames": []})["stage"] == TEXT


def test_invisible_widget():
    # No /anchor path and no checkbox copy: size=invisible wins.
    # (An anchor URL with size=invisible classifies CHECKBOX first —
    # same order as upstream.)
    res = classify_stage({
        "frames": [{"url": "https://www.google.com/recaptcha/api2/frame"
                           "?size=invisible", "visible": True}]})
    assert res.stage == INVISIBLE and res.provider == "recaptcha"


def test_unclassified_provider_hint():
    assert classify_stage({"provider": "recaptcha"})["stage"] == UNKNOWN


class _AttrItem:
    def __init__(self, src="", title="", visible=True):
        self._src = src
        self._title = title
        self._visible = visible

    async def get_attribute(self, name):
        return {"src": self._src, "title": self._title}.get(name, "")

    async def is_visible(self, timeout=None):
        return self._visible


class _FrameLocator:
    def __init__(self, items):
        self._items = items

    async def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]


class _MetaPage:
    """Two visible frames: anchor + open bframe grid popup."""

    def __init__(self):
        self.url = "https://www.google.com/recaptcha/api2/demo"

    def locator(self, sel):
        if "recaptcha" in sel:
            return _FrameLocator([
                _AttrItem("https://www.google.com/recaptcha/api2/anchor?k=K",
                          "reCAPTCHA", True),
                _AttrItem("https://www.google.com/recaptcha/api2/bframe",
                          "", True),
            ])
        return _FrameLocator([])


def test_collect_metadata_then_classify_grid_open():
    import asyncio

    from src.capability.stage import collect_metadata

    meta = asyncio.run(collect_metadata(_MetaPage(), "form text"))
    assert len(meta["frames"]) == 2
    res = classify_stage(meta)
    assert res.stage == IMAGE_GRID and res.provider == "recaptcha"
    assert res.needs_vision and not res.auto_solvable


def test_provider_from_url_table():
    assert provider_from_url("https://js.hcaptcha.com/1/api.js") == "hcaptcha"
    assert provider_from_url(
        "https://newassets.hcaptcha.com/c/frame") == "hcaptcha"
    assert provider_from_url("https://example.test/") == "generic"
    assert provider_from_url("") == "generic"
