"""Runner-helper tests — settle check + credential placeholder picking (offline)."""

from src.runner import credential_placeholder, page_settled


def test_page_settled_blank_and_loading():
    assert page_settled("") is False
    assert page_settled("   ") is False
    assert page_settled("Looking for results") is False
    assert page_settled("LOADING...") is False


def test_page_settled_real_text():
    assert page_settled("The Moon has water ice at the south pole.") is True


def test_page_settled_long_text_with_banner():
    body = "Results. " * 60 + "Looking for results in English? Change to English"
    assert len(body) >= 300
    assert page_settled(body) is True


def test_credential_placeholder_password():
    task = "Sign in with {TWITTER_USERNAME} / {TWITTER_PASSWORD}"
    assert credential_placeholder(task, want_password=True) == "{TWITTER_PASSWORD}"
    assert credential_placeholder(task, want_password=False) == "{TWITTER_USERNAME}"


def test_credential_placeholder_fallbacks():
    assert credential_placeholder("no placeholders here", want_password=True) is None
    assert credential_placeholder("{SOME_KEY}", want_password=True) == "{SOME_KEY}"
    assert credential_placeholder("{SOME_KEY}", want_password=False) == "{SOME_KEY}"
