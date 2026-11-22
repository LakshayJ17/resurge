"""Live progress of one analysis for the website: seven plain-language steps, running counts and a clean feed.

Everything is derived from the run's event log. The feed leaves out pipeline-internal lines (stage markers, file
locations on the server) and flags model-routing lines as technical, so the page can hide them by default.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..outputs.tables import pretty_dimension

# (title, the pipeline stages that belong to it). Order matters: it is the order a run moves through.
PHASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Understand the request", ("Intake",)),
    ("Find competitors", ("Competitor websites", "Competitor discovery")),
    ("Plan the research", ("Research planning",)),
    ("Read and check sources", ("Research & verification", "Gap closing", "Follow-up research")),
    ("Analyse", ("Competitor profiles", "Feature matrix", "Head-to-head", "SWOT", "Market analysis", "QA review",
                 "Risk register")),
    ("Write the report", ("Battlecards", "Executive summary")),
    ("Publish", ("Publishing", "Retrospective")),
)
# Typical seconds per step for a quick run (medians of real runs); deeper runs take proportionally longer.
TYPICAL_S = (10, 10, 20, 300, 420, 200, 35)
DEPTH_SCALE = {"quick": 1.0, "standard": 1.8, "deep": 3.2}
DEPTH_TYPICAL = {"quick": "usually 10–20 minutes", "standard": "usually 25–40 minutes", "deep": "usually about an hour"}
DEPTH_SLOW_S = {"quick": 25 * 60, "standard": 50 * 60, "deep": 80 * 60}  # past this, say the free models are slow

# What each pipeline stage is doing, in words a visitor understands
STEP_TEXT = {
    "Intake": "Reading your request",
    "Competitor websites": "Finding the competitors' websites",
    "Competitor discovery": "Searching for the closest competitors",
    "Research planning": "Writing questions for each company",
    "Research & verification": "Searching, reading pages and checking every fact",
    "Gap closing": "Looking for what is still missing",
    "Follow-up research": "Filling the gaps with another round of reading",
    "Competitor profiles": "Profiling each company",
    "Feature matrix": "Comparing features side by side",
    "Head-to-head": "Comparing each competitor with the company",
    "SWOT": "Comparing each competitor with the company",
    "Market analysis": "Market trends, gaps and recommendations",
    "QA review": "A reviewer is scoring the draft",
    "Risk register": "Listing the risks",
    "Battlecards": "Writing sales battlecards",
    "Executive summary": "Writing the summary and answering your questions",
    "Publishing": "Building the report, PDF and slides",
    "Retrospective": "Wrapping up",
}

_ROUND = re.compile(r"\((?:round|attempt) (\d+)\)")
_PAGES = re.compile(r"fetched (\d+)/\d+ pages")
_VERIFIED = re.compile(r"Batch verified: (\d+)/\d+ usable")
_REUSED = re.compile(r"Reusing (\d+) verified facts")
_HANDOFF = re.compile(r"(\d+) verified facts handed")
_DIMENSIONS = re.compile(r"\(([a-z_]+(?:, [a-z_]+)*)\)$")
_FILE_LINE = re.compile(r"^[a-z_]+: (?:/|[A-Za-z]:\\)")  # "pdf: /srv/reports/…/report.pdf"
_ABS_PATH = re.compile(r"(?:(?<=\s)|^)(?:/[\w.\-]+){2,}/?|[A-Za-z]:\\[^\s]+")
_TECH_AGENTS = ("Model Router", "Chief Technology Officer")
# What a finished step produced, shown under its title: (step, pattern, template)
_RESULTS = (
    (0, re.compile(r"^Brief ready: company in (.+)$"), "{0}"),
    (1, re.compile(r"^Competitive set: (.+)$"), "{0}"),
    (2, re.compile(r"^Plan: (\d+) questions"), "{0} research questions"),
    (3, re.compile(r"^(\d+) verified facts handed"), "{0} verified facts"),
    (4, re.compile(r"^analysis: ([\d.]+)/10"), "Reviewer score {0} of 10"),
    (5, re.compile(r"^Executive summary written"), "Summary and answers written"),
    (6, re.compile(r"^Engagement closed: quality ([\d.]+)/100"), "Quality score {0} of 100"),
)


def stage_name(message: str) -> str:
    return message.removeprefix("▶").strip()


def phase_of(stage: str) -> int | None:
    for i, (_, stages) in enumerate(PHASES):
        if any(stage.startswith(s) for s in stages):
            return i
    return None


def step_text(stage: str) -> str:
    text = next((t for key, t in STEP_TEXT.items() if stage.startswith(key)), stage)
    m = _ROUND.search(stage)
    if m and stage.startswith(("Gap closing", "Follow-up")):
        return f"{text} (round {m.group(1)})"
    if "(revision)" in stage or (m and int(m.group(1)) > 1):
        return f"{text} (second pass)"
    return text


def depth_of(brief_json: str | None) -> str:
    try:
        depth = json.loads(brief_json or "{}").get("depth", "quick")
    except (ValueError, AttributeError):
        depth = "quick"
    return depth if depth in DEPTH_SCALE else "quick"


def clean_message(message: str) -> str:
    """Readable feed text: no stage arrows, dimension keys in words, no file locations."""
    text = stage_name(message)
    if m := _DIMENSIONS.search(text):
        text = text[: m.start()] + "(" + ", ".join(pretty_dimension(d) for d in m.group(1).split(", ")) + ")"
    return _ABS_PATH.sub(lambda m: re.split(r"[/\\]", m.group(0).rstrip("/\\"))[-1], text).strip()


def summarize(events: list[dict[str, Any]], status: str, depth: str = "quick") -> dict[str, Any]:
    scale = DEPTH_SCALE.get(depth, 1.0)
    phases: list[dict[str, Any]] = [{"title": t, "state": "pending", "started": None, "ended": None, "step": "",
                                     "result": "", "expected_s": round(TYPICAL_S[i] * scale)} for i, (t, _) in enumerate(PHASES)]
    counts = {"pages": 0, "facts": 0, "ai_calls": 0}
    verified = reused = 0
    handed: int | None = None
    current: int | None = None
    feed: list[dict[str, Any]] = []

    for e in events:
        kind, agent, message = e.get("kind") or "", str(e.get("agent") or ""), str(e.get("message") or "")
        ts = float(e.get("ts") or 0)
        if kind == "stage":
            stage = stage_name(message)
            p = phase_of(stage)
            if p is None:
                continue
            if current is not None and p != current:
                phases[current]["ended"] = ts
            for skipped in range(0, p):  # a step that never ran (e.g. competitors given) counts as done
                if phases[skipped]["started"] is None:
                    phases[skipped]["state"] = "done"
            if phases[p]["started"] is None:
                phases[p]["started"] = ts
            if current is not None and p != current:
                phases[current]["state"] = "done"
            phases[p]["ended"] = None
            phases[p]["state"] = "active"
            phases[p]["step"] = step_text(stage)
            current = p
            continue
        if kind == "llm":
            counts["ai_calls"] += 1
        for step, pattern, template in _RESULTS:
            if m := pattern.search(message):
                phases[step]["result"] = template.format(*m.groups()).removeprefix("unknown industry · ")[:160]
        if m := _PAGES.search(message):
            counts["pages"] += int(m.group(1))
        if m := _VERIFIED.search(message):
            verified += int(m.group(1))
        if m := _REUSED.search(message):
            reused += int(m.group(1))
        if m := _HANDOFF.search(message):
            handed = int(m.group(1))
        if _FILE_LINE.match(message):
            continue  # where the server wrote each file is nobody else's business
        feed.append({"id": int(e.get("id") or len(feed)), "ts": ts, "agent": agent[:60], "kind": kind,
                     "tech": kind == "llm" or agent.startswith(_TECH_AGENTS),
                     "message": clean_message(message)[:220]})
    counts["facts"] = handed if handed is not None else verified + reused

    last_ts = float(events[-1].get("ts") or 0) if events else None
    if status == "done":
        for ph in phases:
            ph["state"] = "done"
        if current is not None and phases[current]["ended"] is None:
            phases[current]["ended"] = last_ts
    elif status == "failed" and current is not None:
        phases[current]["state"] = "failed"
        phases[current]["ended"] = last_ts
    elif status == "queued":
        for ph in phases:
            ph.update(state="pending", started=None, ended=None, step="", result="")
    return {"phases": phases, "counts": counts, "current": current, "feed": feed,
            "typical": DEPTH_TYPICAL.get(depth, DEPTH_TYPICAL["quick"]), "slow_after_s": DEPTH_SLOW_S.get(depth, 1500)}
