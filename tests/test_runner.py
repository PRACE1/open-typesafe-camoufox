"""Runner-helper tests — settle check + credential placeholder picking (offline)."""

from src.runner import credential_placeholder, loop_guard_trip, page_settled


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


def test_loop_guard_trips_on_third_repeat():
    sig = ("click_item", 0, "https://a.example/")
    trip, run = loop_guard_trip(None, 0, sig)
    assert (trip, run) == (False, 1)
    trip, run = loop_guard_trip(sig, run, sig)
    assert (trip, run) == (False, 2)
    trip, run = loop_guard_trip(sig, run, sig)
    assert (trip, run) == (True, 3)


def test_loop_guard_resets_on_change():
    sig_a = ("click_item", 0, "https://a.example/")
    sig_b = ("click_item", 1, "https://a.example/")
    sig_nav = ("click_item", 0, "https://b.example/")
    _, run = loop_guard_trip(None, 0, sig_a)
    _, run = loop_guard_trip(sig_a, run, sig_a)
    trip, run = loop_guard_trip(sig_a, run, sig_b)
    assert (trip, run) == (False, 1)
    _, run = loop_guard_trip(sig_b, run, sig_a)
    trip, run = loop_guard_trip(sig_a, run, sig_nav)
    assert (trip, run) == (False, 1)
