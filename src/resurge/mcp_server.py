"""Resurge as an MCP server (mcp>=2: ``MCPServer``).

Connect it to Claude Desktop / Claude Code / Cursor / any MCP client:

    {"mcpServers": {"resurge": {"command": "resurge", "args": ["mcp"]}}}

Tools
  research_competitors  - full multi-agent engagement (streams stage progress)
  discover_competitors  - competitive set only (cheap)
  list_runs             - past engagements
  get_report_section    - any section of a saved report as JSON
  get_battlecard        - one competitor's sales battlecard
  search_knowledge_base - verified facts across all runs
  ask_research          - natural-language Q&A over a finished run (RAG), with numbered citations
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from . import __version__
from .config import load_settings
from .schemas import Depth, ResearchBrief, ResearchReport
from .storage import Storage

SECTIONS = ("executive_summary", "profiles", "feature_matrix", "swots", "positioning", "market", "recommendations",
            "risks", "battlecards", "facts", "sources", "metrics", "reviews", "org_chart", "changes_since_last_run",
            "methodology")

INSTRUCTIONS = (
    "Resurge is a multi-agent competitive-intelligence company running on free LLM tiers. Use "
    "research_competitors for a full, fact-checked analysis (minutes; depth 'quick' is fastest), "
    "discover_competitors to identify a competitive set, and ask_research / get_report_section / "
    "search_knowledge_base to answer follow-up questions from verified facts without re-researching. Every fact carries a source URL, "
    "a verbatim quote and a confidence score - cite them."
)


def untrusted_brief(**fields) -> ResearchBrief:
    """Briefs from MCP clients may carry text an upstream model picked up elsewhere: neutralise before use."""
    from .security import clean_snippet, scan

    def clean(v, limit):
        return clean_snippet(v, limit) if isinstance(v, str) else v

    target = clean(fields.get("target") or "", 160)
    if not target or scan(fields.get("target") or "").quarantine:
        raise ValueError("target must be a plain company, product or market name")
    fields["target"] = target
    for key, limit in (("industry", 160), ("competitors", 80), ("focus_areas", 80), ("questions", 300)):
        v = fields.get(key)
        if isinstance(v, list):
            fields[key] = [c for c in (clean(x, limit) for x in v) if c and "[removed" not in c]
        elif isinstance(v, str):
            fields[key] = clean(v, limit)
    return ResearchBrief(**fields)


def _load_report(run_id: str) -> ResearchReport:
    s = load_settings()
    row = Storage(s.db_path).get_run(run_id)
    if not row:
        raise ValueError(f"Unknown run_id {run_id!r} - call list_runs first")
    path = Path(row.get("report_dir") or "") / "report.json"
    if not row.get("report_dir") or not path.exists():
        raise ValueError(f"Run {run_id!r} has no report (status: {row.get('status')}, error: {row.get('error') or '-'})")
    return ResearchReport.model_validate_json(path.read_text())


def build_server() -> MCPServer:
    server = MCPServer(name="resurge", title="Resurge Competitive Intelligence", instructions=INSTRUCTIONS,
                       version=__version__)

    @server.tool(description="Run a full multi-agent competitive-intelligence engagement (research, fact-checking, "
                             "analysis, risk, deliverables). depth: quick | standard | deep. Returns the executive "
                             "summary, scorecard and paths of the generated deck/workbook/HTML report.")
    async def research_competitors(target: str, competitors: list[str] | None = None, depth: str = "quick",
                                   focus_areas: list[str] | None = None, questions: list[str] | None = None,
                                   max_competitors: int = 4, industry: str | None = None,
                                   ctx: Context | None = None) -> dict[str, Any]:
        from .pipeline.orchestrator import ResurgeCompany

        brief = untrusted_brief(target=target, competitors=competitors or [], depth=Depth(depth), industry=industry,
                                focus_areas=focus_areas or [], questions=questions or [], max_competitors=max_competitors)
        loop = asyncio.get_running_loop()
        stages = {"n": 0}

        def on_event(e):
            if ctx is None or e.kind != "stage":
                return
            stages["n"] += 1
            loop.create_task(ctx.report_progress(stages["n"], 20, e.message))

        from .system import keep_awake, release

        awake = keep_awake(tie_to_process=False)
        try:
            result = await ResurgeCompany(load_settings()).run(brief, on_event=on_event)
        finally:
            release(awake)
        r, m = result.report, result.report.metrics
        return {
            "run_id": r.run_id, "headline": r.executive_summary.headline, "summary": r.executive_summary.summary,
            "key_findings": [k.model_dump() for k in r.executive_summary.key_findings],
            "top_threats": r.executive_summary.top_threats, "top_opportunities": r.executive_summary.top_opportunities,
            "competitors": [c.name for c in r.competitors],
            "scorecard": {"quality_score": m.quality_score, "verified_facts": m.facts_usable,
                          "verification_rate": m.verification_rate, "coverage": m.coverage, "qa_score": m.avg_qa_score,
                          "llm_calls": m.llm_calls, "cost_usd": 0.0},
            "artifacts": {k: str(v) for k, v in result.artifacts.items()},
        }

    @server.tool(description="Identify the most relevant competitors of a company/product/market (cheap).")
    async def discover_competitors(target: str, industry: str | None = None, max_competitors: int = 6) -> list[dict]:
        from .pipeline.orchestrator import ResurgeCompany

        comps = await ResurgeCompany(load_settings()).discover_only(
            untrusted_brief(target=target, industry=industry, max_competitors=max_competitors))
        return [c.model_dump() for c in comps]

    @server.tool(description="List past Resurge engagements (run_id, target, status, quality).")
    def list_runs(limit: int = 10) -> list[dict]:
        rows = Storage(load_settings().db_path).list_runs(limit)
        return [{"run_id": r["id"], "target": r.get("target"), "status": r.get("status"),
                 "quality": r.get("quality"), "report_dir": r.get("report_dir")} for r in rows]

    @server.tool(description=f"Return one section of a saved report as JSON. section: {', '.join(SECTIONS)}")
    def get_report_section(run_id: str, section: str = "executive_summary") -> Any:
        if section not in SECTIONS:
            raise ValueError(f"section must be one of {SECTIONS}")
        data = _load_report(run_id).model_dump(mode="json")
        return data.get(section)

    @server.tool(description="Return the sales battlecard for one competitor from a saved run.")
    def get_battlecard(run_id: str, competitor: str) -> dict:
        report = _load_report(run_id)
        for bc in report.battlecards:
            if bc.competitor.lower() == competitor.lower():
                return bc.model_dump(mode="json")
        raise ValueError(f"No battlecard for {competitor!r}; available: {[b.competitor for b in report.battlecards]}")

    @server.tool(description="Search verified facts (supported/partial) across all engagements.")
    def search_knowledge_base(query: str, limit: int = 20) -> list[dict]:
        facts = Storage(load_settings().db_path).search_facts(query, limit)
        keep = ("id", "competitor", "dimension", "claim", "value", "quote", "source_urls", "confidence", "status", "as_of")
        return [{k: f.get(k) for k in keep} for f in facts]

    @server.tool(description="Ask a natural-language question about a finished research run (e.g. 'How much does "
                             "Asana cost for 50 seats?', 'How do we win against Airtable?'). Returns a short answer "
                             "grounded ONLY in that run's verified facts, with numbered citations (claim, quote, "
                             "source URL), a confidence level and suggested follow-ups. run_id defaults to the latest "
                             "finished run. If the research doesn't cover the question, it says so.")
    async def ask_research(question: str, run_id: str | None = None) -> dict:
        from . import rag

        s = load_settings()
        rid = run_id or rag.latest_run_id(s)
        if not rid:
            raise ValueError("No finished run yet - call research_competitors first")
        try:
            return await rag.ask(s, rid, question)
        except rag.RunNotFound as e:
            raise ValueError(f"Unknown or unfinished run {rid!r} - call list_runs first") from e
        except rag.RagUnavailable as e:
            raise ValueError(str(e)) from e

    return server


def serve(transport: str = "stdio", port: int = 8770) -> None:
    server = build_server()
    if transport in ("http", "streamable-http"):
        server.run("streamable-http", host="127.0.0.1", port=port)
    else:
        server.run("stdio")
