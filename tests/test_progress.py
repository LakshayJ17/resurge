"""Live progress for the website: seven steps, honest counts, and a feed that never shows server file paths."""

from __future__ import annotations

import pytest

from resurge.web.progress import PHASES, clean_message, depth_of, step_text, summarize


def ev(i, kind, message, agent="COO", ts=None):
    return {"id": i, "ts": ts if ts is not None else 1000.0 + i, "kind": kind, "agent": agent, "message": message}


RUN = [
    ev(1, "start", "Engagement opened: Swiggy (quick)"),
    ev(2, "stage", "▶ Intake"),
    ev(3, "llm", "Chief of Staff used groq/openai/gpt-oss-120b (900+200 tok, 800 ms)", "Model Router"),
    ev(4, "done", "Brief ready: company in Online food delivery (swiggy.com)", "Chief of Staff"),
    ev(5, "stage", "▶ Competitor discovery"),
    ev(6, "start", "Scanning the market: 4 discovery searches", "Market Scout"),
    ev(7, "done", "Competitive set: Zomato, Blinkit, Zepto", "Market Scout"),
    ev(8, "stage", "▶ Research planning"),
    ev(9, "done", "Plan: 22 questions, 51 queries", "Research Strategist"),
    ev(10, "stage", "▶ Research & verification"),
    ev(11, "info", "Reusing 20 verified facts < 3 days old (saves quota)", "Knowledge Base"),
    ev(12, "info", "Zomato: fetched 4/6 pages, 30 evidence chunks", "Research Lead"),
    ev(13, "start", "Reading 3 sources on Zomato (overview, pricing, products_features)", "Product & Pricing Researcher"),
    ev(14, "info", "Swiggy: fetched 5/6 pages, 41 evidence chunks", "Research Lead"),
    ev(15, "info", "Batch verified: 8/9 usable", "Fact Checker"),
]


def test_a_running_analysis_is_on_the_right_step_with_real_counts():
    p = summarize(RUN, "running")
    states = [ph["state"] for ph in p["phases"]]
    assert states == ["done", "done", "done", "active", "pending", "pending", "pending"]
    assert p["current"] == 3 and p["phases"][3]["step"] == "Searching, reading pages and checking every fact"
    assert p["counts"] == {"pages": 9, "facts": 28, "ai_calls": 1}  # 20 reused + 8 newly verified
    assert p["phases"][0]["result"] == "Online food delivery (swiggy.com)"
    assert p["phases"][1]["result"] == "Zomato, Blinkit, Zepto"
    assert p["phases"][2]["result"] == "22 research questions"
    assert p["phases"][1]["started"] == 1005 and p["phases"][1]["ended"] == 1008
    assert p["phases"][3]["ended"] is None  # still running: the page keeps its clock going


def test_the_feed_is_readable_and_flags_model_calls():
    feed = summarize(RUN, "running")["feed"]
    assert not any(e["kind"] == "stage" for e in feed)  # the steps list shows these
    assert [e["tech"] for e in feed if e["agent"] == "Model Router"] == [True]
    reading = next(e for e in feed if e["message"].startswith("Reading 3 sources"))
    assert reading["message"] == "Reading 3 sources on Zomato (Overview, Pricing, Products & features)"


def test_server_file_paths_never_reach_the_feed():
    events = RUN + [
        ev(20, "stage", "▶ Publishing"),
        ev(21, "info", "pdf: /srv/resurge/reports/20261122-swiggy/report.pdf", "Publishing Desk"),
        ev(22, "info", "site: C:\\resurge\\reports\\x\\site\\index.html", "Publishing Desk"),
        ev(23, "warn", "Could not write /home/app/reports/x/report.xlsx, skipped", "Publishing Desk"),
        ev(24, "done", "Engagement closed: quality 88.5/100, 60 verified facts, 30 LLM calls, $0.00 spent"),
    ]
    p = summarize(events, "done")
    text = " ".join(e["message"] for e in p["feed"])
    assert "/srv/" not in text and "C:\\" not in text and "/home/app" not in text
    assert "Could not write report.xlsx, skipped" in text  # only the file name is left
    assert all(ph["state"] == "done" for ph in p["phases"])  # skipped steps count as done once finished
    assert p["phases"][6]["result"] == "Quality score 88.5 of 100"


def test_failed_and_queued_runs():
    failed = summarize(RUN, "failed")
    assert failed["phases"][3]["state"] == "failed" and failed["phases"][3]["ended"] == 1015
    queued = summarize([], "queued")
    assert all(ph["state"] == "pending" and not ph["started"] for ph in queued["phases"])
    assert queued["counts"] == {"pages": 0, "facts": 0, "ai_calls": 0} and queued["feed"] == []


def test_given_competitors_skip_discovery_and_rounds_are_named():
    events = [ev(1, "stage", "▶ Intake"), ev(2, "stage", "▶ Competitor websites"), ev(3, "stage", "▶ Research planning"),
              ev(4, "stage", "▶ Research & verification"), ev(5, "stage", "▶ Gap closing (round 1)")]
    p = summarize(events, "running")
    assert p["current"] == 3 and p["phases"][3]["step"] == "Looking for what is still missing (round 1)"
    assert step_text("QA review (attempt 2)") == "A reviewer is scoring the draft (second pass)"
    assert step_text("Market analysis (revision)") == "Market trends, gaps and recommendations (second pass)"
    assert step_text("Something new") == "Something new"


@pytest.mark.parametrize("brief,depth", [('{"depth": "standard"}', "standard"), ("{}", "quick"), ("not json", "quick"),
                                         ('{"depth": "huge"}', "quick"), (None, "quick")])
def test_depth_from_the_stored_brief(brief, depth):
    assert depth_of(brief) == depth


def test_deeper_runs_expect_longer_steps():
    quick = summarize(RUN, "running", "quick")["phases"]
    deep = summarize(RUN, "running", "deep")["phases"]
    assert all(d["expected_s"] > q["expected_s"] for q, d in zip(quick, deep, strict=True))
    assert len(quick) == len(PHASES) == 7


def test_clean_message_keeps_prices_and_ratios():
    assert clean_message("▶ Pricing at $10/user/month and 8/9 usable") == "Pricing at $10/user/month and 8/9 usable"
