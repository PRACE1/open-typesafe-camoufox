"""Frontier tests — ledger observe/mark/alias/seed/persist + model guards (offline)."""

import json

from src.decide import _item_option, build_questions
from src.deps import ElementRef
from src.frontier import (
    Frontier,
    extract_seed_pairs,
    extract_urls,
    load_frontier_file,
    normalize_label,
)


def _link(idx, label, href="https://www.google.com/goto?url=TOKEN",
          kind="link"):
    e = ElementRef(idx=idx, kind=kind, label=label, ref=f"e{idx}")
    e.href = href
    return e


def test_normalize_label():
    assert normalize_label("  APHCI\t Private  Group ") == "aphci private group"
    assert normalize_label("") == ""
    assert normalize_label(None) == ""


def test_observe_queues_unseen_links():
    fr = Frontier()
    els = [_link(0, "APHCI"), _link(1, "Amalgamated Plumbing"),
           _link(2, "Search")]
    assert fr.observe(els, step=1) == 3
    assert fr.observe(els, step=2) == 0  # re-probe: nothing new
    assert fr.counts() == {"pending": 3, "visited": 0, "total": 3}


def test_observe_skips_non_links_and_empty_labels():
    fr = Frontier()
    els = [ElementRef(idx=0, kind="button", label="Search"),
           ElementRef(idx=1, kind="link", label="")]
    assert fr.observe(els, step=1) == 0


def test_visit_by_label_alias_kills_serp_reclick():
    """The core loop fix: opaque SERP hrefs resolve via learned labels."""
    fr = Frontier()
    fr.observe([_link(34, "APHCI")], step=1)
    # SERP probe before any visit: not visited.
    assert fr.is_visited_element(_link(99, "APHCI")) is False
    # Click lands on the real group URL with the SERP label attached.
    assert fr.mark_visited("https://www.facebook.com/groups/1784587575124957/",
                           label="APHCI", step=2) is True
    # Next SERP probe (different idx, same opaque href shape): visited.
    assert fr.is_visited_element(_link(34, "APHCI")) is True
    assert fr.is_visited_element(_link(7, "aphci  ")) is True
    # Re-visit bumps the counter (loop signal) but stays visited.
    assert fr.mark_visited("https://www.facebook.com/groups/1784587575124957/",
                           label="APHCI", step=3) is False
    assert fr.counts()["visited"] >= 1


def test_visit_by_real_url():
    fr = Frontier()
    fr.observe([_link(0, "Some Group",
                       href="https://www.facebook.com/groups/123/")], step=1)
    assert fr.mark_visited("https://www.facebook.com/groups/123/",
                           label="Some Group", step=2) is True
    assert fr.is_visited_element(
        _link(5, "Some Group",
              href="https://www.facebook.com/groups/123/")) is True


def test_seed_urls_and_pairs_skip_quietly():
    fr = Frontier()
    assert fr.seed_urls(["https://a.example/x",
                         "https://a.example/x"], step=0) == 1
    assert fr.seed_pairs([("APHCI", "https://www.facebook.com/groups/1/"),
                          ("APHCI", "https://www.facebook.com/groups/1/")],
                         step=0) == 1
    assert fr.is_visited_url("https://a.example/x") is True
    assert fr.is_visited_url("https://other.example/") is False
    # Quiet re-seed emits no new ledger events.
    assert fr.take_events() != []
    assert fr.take_events() == []


def test_extract_urls_and_pairs():
    text = ("Plumbing in Ireland 3.6K https://www.facebook.com/groups/279110886466758/ ; "
            "APHCI 652 https://www.facebook.com/groups/1784587575124957/.")
    urls = extract_urls(text)
    assert urls == ["https://www.facebook.com/groups/279110886466758/",
                    "https://www.facebook.com/groups/1784587575124957/"]
    pairs = extract_seed_pairs(text)
    assert ("plumbing in ireland",
            "https://www.facebook.com/groups/279110886466758/") in pairs
    assert ("aphci",
            "https://www.facebook.com/groups/1784587575124957/") in pairs


def test_snapshot_roundtrip_and_corrupt_tolerance(tmp_path):
    fr = Frontier()
    fr.observe([_link(0, "APHCI")], step=1)
    fr.mark_visited("https://www.facebook.com/groups/1/", label="APHCI",
                    step=2)
    snap = fr.snapshot()
    assert snap["entries"]
    fr2 = Frontier.from_snapshot(snap)
    assert fr2.is_visited_element(_link(9, "APHCI")) is True
    assert fr2.counts() == fr.counts()
    # Corrupt snapshots never kill resume.
    assert Frontier.from_snapshot(None).counts()["total"] == 0
    assert Frontier.from_snapshot({"entries": "junk"}).counts()["total"] == 0
    assert Frontier.from_snapshot(
        {"entries": [{"key": "k"}]}).counts()["total"] == 1
    p = tmp_path / "frontier.json"
    p.write_text(json.dumps(snap), encoding="utf-8")
    assert load_frontier_file(str(p)).counts() == fr.counts()
    assert load_frontier_file(str(tmp_path / "missing.json")
                              ).counts()["total"] == 0


def test_event_ledger_replay(tmp_path):
    fr = Frontier()
    fr.observe([_link(0, "APHCI")], step=1)
    fr.mark_visited("https://www.facebook.com/groups/1/", label="APHCI",
                    step=2)
    events = fr.take_events()
    kinds = [e["event"] for e in events]
    assert "discovered" in kinds and "visited" in kinds
    assert fr.take_events() == []  # drained
    p = tmp_path / "frontier.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    fr2 = Frontier.load_events_file(str(p))
    assert fr2.is_visited_element(_link(4, "APHCI")) is True


def test_to_record_pending_labels_bounded():
    fr = Frontier()
    fr.observe([_link(i, f"Group {i}") for i in range(30)], step=1)
    rec = fr.to_record()
    assert rec["pending"] == 30 and rec["visited"] == 0
    assert len(rec["pending_labels"]) == 12


def test_item_option_marks_frontier_visited():
    fr = Frontier()
    fr.observe([_link(0, "APHCI")], step=1)
    fr.mark_visited("https://www.facebook.com/groups/1/", label="APHCI",
                    step=2)
    opt = _item_option(_link(34, "APHCI"), set(), fr)
    assert opt["visited"] is True
    assert "VISITED" in opt["label"] and "do not propose" in opt["label"]
    opt2 = _item_option(_link(35, "Fresh Group"), set(), fr)
    assert opt2["visited"] is False
    assert "ALREADY VISITED" not in opt2["label"]


def test_build_questions_threads_frontier():
    fr = Frontier()
    fr.observe([_link(0, "APHCI")], step=1)
    fr.mark_visited("https://www.facebook.com/groups/1/", label="APHCI",
                    step=2)
    q = build_questions([_link(34, "APHCI"), _link(35, "Fresh")],
                        ["https://a.example/"], frontier=fr)
    assert q["item"]["criteria"]["e34"]["visited"] is True
    assert q["item"]["criteria"]["e35"]["visited"] is False


def test_proposal_executable_vetoes_visited():
    from src.runner.proposal import proposal_executable
    from src.writer import ProposedAction

    fr = Frontier()
    fr.observe([_link(34, "APHCI")], step=1)
    fr.mark_visited("https://www.facebook.com/groups/1/", label="APHCI",
                    step=2)
    els = [_link(34, "APHCI"), _link(35, "Fresh Group")]
    repeat = ProposedAction(question="q?", kind="click_item", item=34,
                            url=None, rationale="r")
    assert proposal_executable(repeat, els, fr) is False
    fresh = ProposedAction(question="q?", kind="click_item", item=35,
                           url=None, rationale="r")
    assert proposal_executable(fresh, els, fr) is True
    # No frontier: old behavior preserved.
    assert proposal_executable(repeat, els) is True
    goto_done = ProposedAction(question="q?", kind="goto",
                               item=None, url="https://a.example/x",
                               rationale="r")
    fr2 = Frontier()
    fr2.seed_urls(["https://a.example/x"])
    assert proposal_executable(goto_done, els, fr2) is False
    goto_new = ProposedAction(question="q?", kind="goto",
                              item=None, url="https://b.example/",
                              rationale="r")
    assert proposal_executable(goto_new, els, fr2) is True


def test_writer_marks_visited_and_lists_pending():
    from src.writer import summarize_elements

    fr = Frontier()
    fr.observe([_link(34, "APHCI"), _link(35, "Fresh Group")], step=1)
    fr.mark_visited("https://www.facebook.com/groups/1/", label="APHCI",
                    step=2)
    text = summarize_elements([_link(34, "APHCI"), _link(35, "Fresh Group")],
                              fr)
    assert "VISITED" in text
    assert "Fresh Group" in text


def test_dead_marks_threshold_and_host_scoping():
    fr = Frontier()
    el = _link(0, "I'm not a robot", href="")
    assert fr.dead_count(el) == 0
    assert fr.is_dead_element(el) is False
    assert fr.mark_dead("challenge", "I'm not a robot", "", step=1) == 1
    assert fr.is_dead_element(el) is False  # threshold is 2
    assert fr.mark_dead("challenge", "I'm not a robot", "", step=2) == 2
    assert fr.is_dead_element(el) is True
    # Same label on another host with a real href is a different key.
    other = _link(1, "I'm not a robot",
                  href="https://other.example/captcha")
    assert fr.is_dead_element(other) is False
    assert "DEAD" in fr.element_mark(el)
    assert fr.element_mark(other) == ""


def test_dead_mark_visible_in_prompts():
    from src.decide import _item_option
    from src.writer import summarize_elements

    fr = Frontier()
    els = [_link(0, "I'm not a robot", href=""),
           _link(1, "Fresh Group")]
    fr.mark_dead("challenge", "I'm not a robot", "", step=1)
    fr.mark_dead("challenge", "I'm not a robot", "", step=2)
    opt = _item_option(els[0], set(), fr)
    assert "DEAD" in opt["label"] and "do not pick" in opt["label"]
    assert "DEAD" not in _item_option(els[1], set(), fr)["label"]
    text = summarize_elements(els, fr)
    assert "DEAD" in text
    # Visited marks still work alongside dead marks.
    fr2 = Frontier()
    fr2.mark_visited("https://www.facebook.com/groups/1/", label="APHCI",
                     step=1)
    assert "VISITED" in fr2.element_mark(_link(9, "APHCI"))
    assert fr2.element_mark(_link(9, "Elsewhere")) == ""


def test_dead_persists_snapshot_and_clears():
    fr = Frontier()
    fr.mark_dead("challenge", "I'm not a robot", "", step=3)
    fr.mark_dead("challenge", "I'm not a robot", "", step=4)
    snap = fr.snapshot()
    assert snap["dead"]
    fr2 = Frontier.from_snapshot(snap)
    assert fr2.is_dead_element(_link(0, "I'm not a robot", href="")) is True
    rec = fr.to_record()
    assert rec["dead_actions"] and rec["dead_actions"][0]["count"] == 2
    fr2.clear_dead()
    assert fr2.is_dead_element(_link(0, "I'm not a robot", href="")) is False


def test_labels_match_prefix_drift_and_guards():
    from src.frontier import labels_match

    assert labels_match("aphci", "aphci facebook https://x � groups")
    assert labels_match("aphci facebook https://x � groups",
                        "aphci facebook https://x � groups")
    # Snippet-tail drift between probes still matches.
    assert labels_match("aphci facebook https://x � facebook groups",
                        "aphci facebook https://x � facebook groups extra tail words")
    # Different groups sharing head words do NOT match.
    assert not labels_match("plumbing in ireland- advice, work",
                            "plumbers in ireland jobs")
    # Short chrome labels never match (blast-radius guard).
    assert not labels_match("home", "home page")
    assert not labels_match("join", "join group")
    assert not labels_match("", "anything")
    # Exact equality always matches — every ledger lookup depends on it.
    assert labels_match("abcde", "abcde")


def test_seed_short_label_blocks_long_serp_probe():
    """Live regression (20260918-081500 steps 1→5): steer seed "aphci"
    must veto the long SERP probe label through opaque redirect hrefs."""
    fr = Frontier()
    fr.seed_pairs([("aphci",
                    "https://www.facebook.com/groups/1784587575124957/")],
                  step=1)
    probe = _link(25, "APHCI Facebook https://www.facebook.com � Facebook Groups")
    assert fr.is_visited_element(probe) is True
    assert "VISITED" in fr.element_mark(probe)


def test_visit_label_drift_across_probes():
    """A visit recorded under one rendering blocks a drifted re-render."""
    fr = Frontier()
    fr.mark_visited("https://www.facebook.com/groups/1/",
                    label="Amalgamated Plumbing And Heating contractors Group "
                          "Facebook https://www.facebook.com � Facebook Groups",
                    step=1)
    drifted = _link(38, "Amalgamated Plumbing And Heating contractors Group "
                        "Facebook https://www.facebook.com � Facebook Groups "
                        "new snippet tail here")
    assert fr.is_visited_element(drifted) is True


def test_dead_counts_sum_across_drifted_marks():
    fr = Frontier()
    fr.mark_dead("click_item", "InnerCity Plumbing Dublin", "www.google.com",
                 step=1)
    assert fr.is_dead_element(
        _link(3, "InnerCity Plumbing Dublin see more posts here",
              href="https://www.google.com/goto?url=T")) is False
    fr.mark_dead("click_item", "InnerCity Plumbing Dublin latest updates",
                 "www.google.com", step=2)
    assert fr.is_dead_element(
        _link(3, "InnerCity Plumbing Dublin see more posts here",
              href="https://www.google.com/goto?url=T")) is True


def test_pending_excludes_dead_targets():
    fr = Frontier()
    fr.observe([_link(0, "InnerCity Plumbing Dublin"), _link(1, "Fresh")],
               step=1)
    assert "InnerCity Plumbing Dublin" in fr.pending_labels()
    fr.mark_dead("click_item", "InnerCity Plumbing Dublin", "", step=2)
    fr.mark_dead("click_item", "InnerCity Plumbing Dublin", "", step=3)
    assert "InnerCity Plumbing Dublin" not in fr.pending_labels()
    assert "Fresh" in fr.pending_labels()


def test_dead_revisit_vetoes_writer_proposal():
    from src.runner.proposal import proposal_executable
    from src.writer import ProposedAction

    fr = Frontier()
    els = [_link(0, "I'm not a robot", href="")]
    fr.mark_dead("challenge", "I'm not a robot", "", step=1)
    fr.mark_dead("challenge", "I'm not a robot", "", step=2)
    repeat = ProposedAction(question="q?", kind="click_item", item=0,
                            url=None, rationale="r")
    assert proposal_executable(repeat, els, fr) is False
