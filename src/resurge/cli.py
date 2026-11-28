"""Resurge command line.

    resurge init                      # create .env and show where to get FREE keys
    resurge doctor                    # check providers, keys, models, search backends
    resurge run "Notion" -c "Coda,ClickUp" --depth standard
    resurge discover "Linear"         # just the competitive set
    resurge org | models | runs | learn | kb "pricing"
    resurge watch "Notion" --interval-hours 24
    resurge publish <run_id> --sheets
    resurge index [RUN_ID]            # index a finished run (or all) for RAG chat
    resurge chat [RUN_ID]             # ask questions about a run, answers cite verified facts
    resurge mcp                       # expose Resurge as an MCP server (Claude, Cursor, ...)
    resurge ui                        # local web dashboard
"""

from __future__ import annotations

import asyncio
import sys
import re
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .config import PROJECT_ROOT, load_settings
from .events import ANALYSIS, COMMS, EXEC, FINANCE, INFRA, LEARNING, QA, RESEARCH, RISK, STRATEGY, Event
from .schemas import Depth, ResearchBrief

app = typer.Typer(help="Resurge - a free, multi-agent AI competitive-intelligence company.",
                  no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
console = Console()

DEPT_STYLE = {EXEC: "bold magenta", STRATEGY: "cyan", RESEARCH: "blue", QA: "green", ANALYSIS: "yellow",
              RISK: "red", COMMS: "bright_magenta", LEARNING: "bright_cyan", INFRA: "dim", FINANCE: "bright_green"}
KIND_ICON = {"start": ">", "done": "✓", "warn": "!", "error": "✗", "review": "*", "handoff": ">>", "stage": "▶",
             "info": "-", "llm": "~"}


def _settings(local: bool = False, private: bool = False, providers: str = "", no_cache: bool = False):
    s = load_settings()
    if local:
        s.local_only = True
    if private:
        s.private_mode = True
    if providers:
        s.providers_allow = [p.strip() for p in providers.split(",") if p.strip()]
    if no_cache:
        s.use_cache = False
    return s


def _printer(verbose: bool):
    def on_event(e: Event) -> None:
        if e.kind == "llm" and not verbose:
            return
        style = DEPT_STYLE.get(e.department, "white")
        icon = KIND_ICON.get(e.kind, "-")
        ts = time.strftime("%H:%M:%S", time.localtime(e.ts))
        model = f" [dim]({e.data['model']})[/dim]" if e.data.get("model") else ""
        msg = e.message.replace("[", "\\[")
        if e.kind == "stage":
            console.rule(f"[bold]{msg}[/bold] [dim]{e.department}[/dim]", style=style)
            return
        level = {"warn": "yellow", "error": "bold red"}.get(e.kind, "")
        body = f"[{level}]{msg}[/{level}]" if level else msg
        console.print(f"[dim]{ts}[/dim] [{style}]{icon} {e.agent}[/{style}] {body}{model}")
    return on_event


def _keep_awake() -> bool:
    from .system import keep_awake

    return keep_awake(tie_to_process=True) is not None


_TERM_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _term_safe(text: str) -> str:
    """Text for the terminal with control characters removed: research text comes from the web, and escape sequences
    could rewrite the clipboard, the window title or hide text (OSC 52, OSC 0, CSI)."""
    return _TERM_CONTROL.sub("", str(text or ""))


def _split(x: str) -> list[str]:
    """Comma-separated list; use ';' instead when names contain commas ("Acme, Inc.;Beta Corp")."""
    sep = ";" if ";" in (x or "") else ","
    return [p.strip() for p in (x or "").split(sep) if p.strip()]


# --------------------------------------------------------------------------- #
@app.command()
def run(
    target: str = typer.Argument("", help="Company, product or market to research for (e.g. 'Notion')"),
    brief_file: Optional[Path] = typer.Option(None, "--brief", help="YAML brief (see examples/briefs); CLI flags override"),
    competitors: str = typer.Option("", "--competitors", "-c", help="Comma-separated competitors (else auto-discovered)"),
    url: Optional[str] = typer.Option(None, help="Target's website"),
    industry: Optional[str] = typer.Option(None, help="Industry / category"),
    region: str = typer.Option("global", help="Market region"),
    focus: str = typer.Option("", help="Comma-separated focus areas, e.g. 'pricing,AI features'"),
    question: list[str] = typer.Option([], "--question", "-q", help="Custom client question (repeatable)"),
    depth: Depth = typer.Option(Depth.standard, help="quick | standard | deep (quota vs thoroughness)"),
    max_competitors: int = typer.Option(4, "--max", "-n", help="Max competitors to auto-discover"),
    audience: str = typer.Option("executive team", help="Who reads the deliverables"),
    out: Optional[Path] = typer.Option(None, help="Output directory (default reports/<run_id>)"),
    formats: str = typer.Option("", help="Subset: charts,site,html,md,pdf,pptx,json (default all)"),
    local: bool = typer.Option(False, help="Use only local Ollama models (100% private, unlimited)"),
    private: bool = typer.Option(False, help="Exclude providers whose free tier may train on prompts"),
    providers: str = typer.Option("", help="Restrict to providers, e.g. 'groq,gemini'"),
    no_cache: bool = typer.Option(False, help="Bypass caches"),
    reuse_days: float = typer.Option(3.0, help="Reuse verified KB facts younger than N days (0 = off)"),
    sheets: bool = typer.Option(False, help="Also publish to Google Sheets (needs service account)"),
    notify: bool = typer.Option(False, help="Send Slack/Discord/webhook summary"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show every LLM call"),
    keep_awake: bool = typer.Option(True, help="macOS: stop idle sleep from stalling the run (caffeinate)"),
):
    """Run a full competitive-intelligence engagement."""
    from .pipeline.orchestrator import ResurgeCompany

    s = _settings(local, private, providers, no_cache)
    data: dict = {}
    if brief_file:
        import yaml

        data = yaml.safe_load(brief_file.read_text()) or {}
    cli = {"target": target or None, "target_url": url, "industry": industry,
           "region": region if region != "global" else None, "competitors": _split(competitors) or None,
           "max_competitors": max_competitors if max_competitors != 4 else None, "focus_areas": _split(focus) or None,
           "questions": list(question) or None, "depth": depth if depth != Depth.standard else None,
           "audience": audience if audience != "executive team" else None}
    data.update({k: v for k, v in cli.items() if v is not None})
    if not data.get("target"):
        console.print("[red]A target is required (argument or 'target:' in --brief).[/red]")
        raise typer.Exit(2)
    brief = ResearchBrief.model_validate(data)
    depth = brief.depth
    target = brief.target
    company = ResurgeCompany(s)
    console.print(Panel.fit(f"[bold]Resurge[/bold] engagement: [cyan]{target}[/cyan], depth [yellow]{depth.value}[/yellow]"
                            + (", [green]local-only[/green]" if s.local_only else "")
                            + (", [green]private[/green]" if s.private_mode else ""), border_style="magenta"))
    if keep_awake and _keep_awake():
        console.print("[dim]Keeping the Mac awake while this run is active (lid-close still sleeps).[/dim]")
    try:
        result = asyncio.run(company.run(brief, out_dir=out, formats=set(_split(formats)) or None,
                                         on_event=_printer(verbose), reuse_days=reuse_days, sheets=sheets, notify=notify))
    except RuntimeError as e:
        console.print(f"[bold red]✗ {e}[/bold red]")
        raise typer.Exit(1) from e
    except KeyboardInterrupt:
        console.print("[yellow]Run cancelled - marked as 'cancelled' in `resurge runs`.[/yellow]")
        raise typer.Exit(130) from None
    _summary(result)


def _summary(result) -> None:
    r, m = result.report, result.report.metrics
    console.rule("[bold magenta]Deliverables")
    if r.executive_summary.headline:
        console.print(Panel(f"[bold]{r.executive_summary.headline}[/bold]\n\n{r.executive_summary.summary}",
                            title="Executive summary", border_style="magenta"))
    t = Table(title="Run scorecard", show_header=False, box=None)
    for k, v in [("Quality score", f"{m.quality_score}/100"),
                 ("Verified facts", f"{m.facts_usable}/{m.facts_total} ({m.verification_rate:.0%}), grounding {m.grounding_rate:.0%}"),
                 ("Coverage", f"{m.coverage:.0%} of competitor × dimension cells"),
                 ("QA score", f"{m.avg_qa_score}/10"),
                 ("LLM calls", f"{m.llm_calls} (+{m.cache_hits} cache hits, {m.llm_failures} failovers)"),
                 ("Tokens", f"{m.tokens_in + m.tokens_out:,}"),
                 ("Searches / pages", f"{m.searches} / {m.pages_fetched}"),
                 ("Cost", f"$0.00 (about ${m.estimated_paid_cost_usd:.2f} on a paid frontier API)"),
                 ("Duration", f"{m.duration_s:.0f}s")]:
        t.add_row(f"[dim]{k}[/dim]", v)
    console.print(t)
    a = Table(title="Artifacts", show_header=False, box=None)
    for k, v in result.artifacts.items():
        a.add_row(f"[{'red' if k.endswith('_error') else 'cyan'}]{k}[/]", str(v))
    console.print(a)
    if result.new_lessons:
        console.print("[bright_cyan]New playbook lessons:[/bright_cyan]")
        for lesson in result.new_lessons:
            console.print(f"  [dim]{lesson.id}[/dim] ({lesson.role}) {lesson.text}")
    if r.changes_since_last_run:
        console.print(f"[yellow]{len(r.changes_since_last_run)} changes since last run[/yellow] (see report)")
    page = result.artifacts.get("html") or result.artifacts.get("site")  # the long report page is the report
    if page and not str(page).endswith("_error"):
        console.print(f"\n[bold]Open the report:[/bold] {Path(str(page)).resolve().as_uri()}")
        console.print(f"[bold]Ask it questions:[/bold] resurge chat {r.run_id}   (or the Ask page via `resurge ui`)")


# --------------------------------------------------------------------------- #
@app.command()
def discover(target: str, industry: Optional[str] = None, n: int = typer.Option(6, "--max", "-n"),
             local: bool = False, providers: str = ""):
    """Identify the competitive set only (cheap)."""
    from .pipeline.orchestrator import ResurgeCompany

    company = ResurgeCompany(_settings(local, providers=providers))
    comps = asyncio.run(company.discover_only(ResearchBrief(target=target, industry=industry, max_competitors=n),
                                              on_event=_printer(False)))
    t = Table(title=f"Competitors of {target}")
    for col in ("Name", "Website", "Category", "Rationale"):
        t.add_column(col)
    for c in comps:
        t.add_row(c.name, c.website or "-", c.category, c.rationale or "")
    console.print(t)


@app.command()
def doctor(ping: bool = typer.Option(False, help="Send a tiny test request to each model"),
           local: bool = False, providers: str = ""):
    """Check API keys, live model availability, today's quota usage and search backends."""
    from .llm import Registry
    from .llm.doctor import run_doctor
    from .storage import Storage

    s = _settings(local, providers=providers)
    s.ensure_dirs()
    st = Storage(s.db_path)
    reg = Registry.load(s.models_file)
    rows = asyncio.run(run_doctor(reg, s, st, ping=ping))
    t = Table(title="LLM providers (free tiers)")
    for col in ("Provider", "Status", "Models", "Missing", "Trains on data", "Key env", "Get a free key"):
        t.add_column(col, overflow="fold")
    for r in rows:
        ok = str(r["status"]).startswith("ok")
        t.add_row(r["provider"], f"[{'green' if ok else 'yellow'}]{r['status']}[/]", str(r["models"]),
                  ", ".join(r["missing"]) or "-", "yes" if r["trains_on_data"] else "no", r["key_env"], r["signup"])
    console.print(t)
    for r in rows:
        if r.get("usage_today"):
            console.print(f"[dim]{r['provider']} usage today:[/dim] " + "; ".join(f"{k}: {v}" for k, v in r["usage_today"].items()))
        if r.get("ping"):
            console.print(f"[dim]{r['provider']} ping:[/dim] " + "; ".join(f"{k}: {v}" for k, v in r["ping"].items()))
    try:
        from .tools import SearchRouter
        router = SearchRouter()
        st2 = Table(title="Search & data backends")
        st2.add_column("Kind")
        st2.add_column("Available backends (priority order)")
        for kind in ("web", "news", "community", "reference", "code"):
            st2.add_row(kind, ", ".join(b.name for b in router.available_backends(kind)) or "-")
        console.print(st2)
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]Search backends unavailable: {e}[/yellow]")
    active = [r for r in rows if str(r["status"]).startswith("ok")]
    if not active:
        console.print("[bold yellow]No LLM provider is ready.[/bold yellow] Run [cyan]resurge init[/cyan] and add at least "
                      "one free key (Groq, Gemini or OpenRouter take ~1 minute), or start Ollama.")


@app.command()
def models(role: Optional[str] = typer.Option(None, help="Show routing order for one role")):
    """List registered models with limits, health, usage and learned performance."""
    from .budget import Budget
    from .llm import LLMGateway, Registry
    from .storage import Storage

    s = _settings()
    s.ensure_dirs()
    st = Storage(s.db_path)
    reg = Registry.load(s.models_file)
    asyncio.run(reg.discover(s, cache=st))
    gw = LLMGateway(reg, st, s, budget=Budget())
    if role:
        cands = gw.candidates(role, 2000)
        console.print(f"[bold]{role}[/bold] routing order now: " + ", ".join(m.id for m in cands[:8]) or "(none)")
        asyncio.run(gw.aclose())
        return
    health = st.all_health()
    stats: dict[str, list[dict]] = {}
    for row in st.get_model_stats():
        stats.setdefault(row["model_id"], []).append(row)
    t = Table(title="Model registry")
    for col in ("Model", "Family", "Tags", "Prior", "RPM/RPD/TPM", "Active", "Health", "Learned"):
        t.add_column(col, overflow="fold")
    for m in sorted(reg.models.values(), key=lambda m: (m.provider, -m.quality)):
        h = health.get(m.id, {})
        dis = (h.get("disabled_until") or 0) > time.time()
        sts = stats.get(m.id, [])
        calls = sum(x["calls"] for x in sts)
        succ = sum(x["successes"] for x in sts)
        learned = f"{succ / calls:.0%} of {calls}" if calls else "-"
        t.add_row(m.id, m.family, ",".join(sorted(m.tags)), f"{m.quality:.2f}",
                  f"{m.rpm or '-'}/{m.rpd or '-'}/{m.tpm or '-'}", "✓" if m.provider in gw.active else "",
                  f"[red]disabled: {h.get('reason', '')[:40]}[/red]" if dis else "ok", learned)
    console.print(t)
    asyncio.run(gw.aclose())


@app.command()
def org():
    """Show the company org chart and which free models each role routes to right now."""
    from .agents.org import ORG
    from .budget import Budget
    from .llm import LLMGateway, Registry
    from .storage import Storage

    s = _settings()
    s.ensure_dirs()
    st = Storage(s.db_path)
    reg = Registry.load(s.models_file)
    asyncio.run(reg.discover(s, cache=st))
    gw = LLMGateway(reg, st, s, budget=Budget())
    t = Table(title="Resurge org chart")
    for col in ("Department", "Agent", "Mission", "Routes to (now)"):
        t.add_column(col, overflow="fold")
    for p in ORG.values():
        if p.role:
            cands = [m.id for m in gw.candidates(p.role, 2000)[:3]]
            route = ", ".join(cands) if cands else "[yellow]no model available[/yellow]"
        else:
            route = "[dim]deterministic code (0 tokens)[/dim]"
        t.add_row(f"[{DEPT_STYLE.get(p.department, 'white')}]{p.department}[/]", p.title, p.mission, route)
    console.print(t)
    asyncio.run(gw.aclose())


@app.command()
def runs(limit: int = 20):
    """List past engagements."""
    from .storage import Storage

    s = _settings()
    s.ensure_dirs()
    t = Table(title="Engagements")
    for col in ("Run", "Target", "Status", "Quality", "Facts", "LLM calls", "Report"):
        t.add_column(col, overflow="fold")
    for r in Storage(s.db_path).list_runs(limit):
        m = r.get("metrics") or {}
        t.add_row(r["id"], r.get("target") or "", r.get("status") or "", f"{r.get('quality') or 0:.0f}",
                  str(m.get("facts_usable", "")), str(m.get("llm_calls", "")), r.get("report_dir") or "")
    console.print(t)


@app.command()
def learn(role: Optional[str] = None):
    """What the company has learned: playbooks, model leaderboard, source reputation, quality trend."""
    from .storage import Storage

    s = _settings()
    s.ensure_dirs()
    st = Storage(s.db_path)
    lessons = [x for x in st.all_lessons() if not role or x["role"] == role]
    t = Table(title="Playbook (ACE-style lessons)")
    for col in ("ID", "Role", "Lesson", "+", "-", "Uses"):
        t.add_column(col, overflow="fold")
    for x in lessons:
        t.add_row(x["id"], x["role"], x["text"], str(x["helpful"]), str(x["harmful"]), str(x["uses"]))
    console.print(t if lessons else "[dim]No lessons yet - they appear after the first engagement.[/dim]")
    ms = Table(title="Model leaderboard (by role)")
    for col in ("Role", "Model", "Calls", "Success", "Validated quality", "Avg latency"):
        ms.add_column(col)
    for r in st.get_model_stats():
        if role and r["role"] != role:
            continue
        q = f"{r['quality_sum'] / r['quality_n']:.2f}" if r["quality_n"] else "-"
        lat = f"{r['latency_ms_total'] / max(1, r['calls']) / 1000:.1f}s" if r["calls"] else "-"
        ms.add_row(r["role"], r["model_id"], str(r["calls"]), f"{r['successes'] / max(1, r['calls']):.0%}", q, lat)
    console.print(ms)
    src = Table(title="Source reputation (learned from verification)")
    for col in ("Domain", "Supported", "Partial", "Unsupported", "Reliability"):
        src.add_column(col)
    for r in st.top_sources(15):
        src.add_row(r["domain"], str(r["supported"]), str(r["partial"]), str(r["unsupported"]),
                    f"{st.source_reliability(r['domain'], 0.5):.2f}")
    console.print(src)
    hist = st.score_history("quality_score", 20)
    if hist:
        bars = "▁▂▃▄▅▆▇█"
        line = "".join(bars[min(7, int(v / 100 * 8))] for _, v in hist)
        console.print(f"Quality trend (last {len(hist)} runs): [bold]{line}[/bold]  latest {hist[-1][1]:.0f}/100")


@app.command()
def kb(query: str, limit: int = 20):
    """Search the verified-facts knowledge base."""
    from .storage import Storage

    s = _settings()
    s.ensure_dirs()
    t = Table(title=f"Knowledge base: '{query}'")
    for col in ("Fact", "Competitor", "Dimension", "Claim", "Conf", "Source"):
        t.add_column(col, overflow="fold")
    for f in Storage(s.db_path).search_facts(query, limit):
        t.add_row(f["id"], f["competitor"], f["dimension"], f["claim"], f"{f.get('confidence', 0):.2f}",
                  (f.get("source_urls") or [""])[0])
    console.print(t)


@app.command()
def publish(run_id: str, formats: str = typer.Option("", help="Subset of formats"),
            sheets: bool = False, notify: bool = False):
    """Re-render deliverables from a saved run (e.g. push to Google Sheets later)."""
    from . import outputs
    from .schemas import ResearchReport
    from .storage import Storage

    s = _settings()
    run_row = Storage(s.db_path).get_run(run_id)
    if not run_row or not run_row.get("report_dir"):
        console.print(f"[red]Unknown run {run_id}[/red]")
        raise typer.Exit(1)
    out_dir = Path(run_row["report_dir"])
    if not (out_dir / "report.json").exists():
        console.print(f"[red]Run {run_id} has no report.json (status: {run_row.get('status')}); nothing to publish.[/red]")
        raise typer.Exit(1)
    report = ResearchReport.model_validate_json((out_dir / "report.json").read_text())
    fmts = set(_split(formats)) or None
    if sheets:
        fmts = (fmts or set(outputs.DEFAULT_FORMATS)) | {"gsheets"}
    arts = outputs.publish_all(report, out_dir, fmts)
    if notify:
        outputs.notify(report, arts)
    for k, v in arts.items():
        console.print(f"[cyan]{k}[/cyan]: {v}")


@app.command()
def index(run_id: Optional[str] = typer.Argument(None, help="Run to index (default: every finished run)")):
    """Index finished runs for RAG chat (local embeddings + Qdrant; idempotent)."""
    from . import rag
    from .storage import Storage

    s = _settings()
    s.ensure_dirs()
    if not rag.available():
        console.print(f"[red]RAG chat needs the optional extra:[/red] {rag.INSTALL_HINT}")
        raise typer.Exit(1)
    st = Storage(s.db_path)
    ids = [run_id] if run_id else [r["id"] for r in st.list_runs(500) if r.get("status") == "done"]
    if not ids:
        console.print("[yellow]No finished runs to index.[/yellow]")
        return
    t = Table(title="RAG index")
    for col in ("Run", "Chunks", "Backend", "Seconds"):
        t.add_column(col)
    failed = 0
    for rid in ids:
        try:
            report = rag.load_report(s, rid)
            with console.status(f"Indexing {rid}..."):
                stats = rag.index_run(s, report, storage=st)
            t.add_row(rid, str(stats["chunks"]), stats["backend"], f"{stats['seconds']:.1f}")
        except rag.RunNotFound as e:
            failed += 1
            t.add_row(rid, "-", f"[yellow]{e}[/yellow]", "-")
        except rag.RagUnavailable as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
    console.print(t)
    if failed and run_id:
        raise typer.Exit(1)


@app.command()
def chat(run_id: Optional[str] = typer.Argument(None, help="Run to chat with (default: latest finished run)")):
    """Ask questions about a finished run; answers cite verified facts and their sources (/exit to quit)."""
    from rich.markdown import Markdown
    from rich.text import Text

    from . import rag

    s = _settings()
    s.ensure_dirs()
    if not rag.available():
        console.print(f"[red]RAG chat needs the optional extra:[/red] {rag.INSTALL_HINT}")
        raise typer.Exit(1)
    run_id = run_id or rag.latest_run_id(s)
    if not run_id:
        console.print("[yellow]No finished run found - run `resurge run <target>` first.[/yellow]")
        raise typer.Exit(1)
    try:
        report = rag.load_report(s, run_id)
    except rag.RunNotFound as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    async def repl() -> None:
        st = rag.status(s, run_id)
        if not st["indexed"]:
            with console.status("Indexing this run (first time only)..."):
                st = await asyncio.to_thread(rag.index_run, s, report)
        header = Text.assemble(("Resurge chat", "bold magenta"), ": ", (report.brief.target, "cyan"), " vs ",
                               ", ".join(c.name for c in report.competitors) or "-",
                               f"\nrun {run_id}, {st['chunks']} chunks, {st['backend']} store. Type /exit to quit")
        console.print(Panel(header, border_style="magenta"))
        llm = await rag.make_gateway(s, run_id)
        history: list[dict] = []
        try:
            while True:
                try:
                    question = (await asyncio.to_thread(console.input, "[bold cyan]You >[/bold cyan] ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not question:
                    continue
                if question.lower() in ("/exit", "/quit", "exit", "quit", ":q"):
                    break
                t0 = time.time()
                try:
                    with console.status("Thinking..."):
                        out = await rag.ask(s, run_id, question[: rag.MAX_QUESTION], history, llm=llm)
                except Exception as e:  # noqa: BLE001 - keep the REPL alive; never echo provider bodies
                    msg = str(e) if isinstance(e, rag.RagUnavailable) else type(e).__name__
                    console.print(Text(f"✗ Could not answer: {msg}", style="red"))
                    continue
                style = {"high": "green", "medium": "yellow", "low": "red"}[out["confidence"]]
                console.print(Panel(Markdown(_term_safe(out["answer"]), hyperlinks=False), border_style=style,
                                    title=f"confidence {out['confidence']}"
                                          + ("" if out["grounded"] else " (not covered)"),
                                    subtitle=f"{time.time() - t0:.1f}s"))
                for c in out["citations"]:
                    line = Text(f"[{c['n']}] ", style="bold")
                    line.append(_term_safe(c["claim"]))
                    if c.get("competitor"):
                        line.append(f" ({_term_safe(c['competitor'])})", style="cyan")
                    if c.get("url"):
                        line.append(f"\n    {_term_safe(c.get('title') or '')} {_term_safe(c['url'])}".rstrip(), style="dim")
                    console.print(line)
                if out["followups"]:
                    console.print(Text("Try: " + "  |  ".join(_term_safe(f) for f in out["followups"]),
                                       style="dim italic"))
                history += [{"role": "user", "content": question}, {"role": "assistant", "content": out["answer"]}]
                history = history[-rag.MAX_TURNS:]
        finally:
            await rag._close(llm)

    try:
        asyncio.run(repl())
    except rag.RagUnavailable as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@app.command()
def watch(target: str, competitors: str = typer.Option("", "--competitors", "-c"),
          interval_hours: float = typer.Option(0, help="Repeat every N hours (0 = run once and exit)"),
          depth: Depth = Depth.quick, notify: bool = True, local: bool = False):
    """Competitor monitoring: re-run and report what changed since the last engagement."""
    from .pipeline.orchestrator import ResurgeCompany

    company = ResurgeCompany(_settings(local))
    _keep_awake()
    while True:
        brief = ResearchBrief(target=target, competitors=_split(competitors), depth=depth)
        try:
            result = asyncio.run(company.run(brief, on_event=_printer(False), reuse_days=0, notify=notify))
            changes = result.report.changes_since_last_run
            console.print(Panel("\n".join(changes) or "No material changes detected.",
                                title=f"Changes since last run ({len(changes)})", border_style="yellow"))
        except KeyboardInterrupt:
            raise typer.Exit(130) from None
        except Exception as e:  # noqa: BLE001 - one bad iteration must not end monitoring
            console.print(f"[red]This monitoring run failed: {e}[/red]")
            if interval_hours <= 0:
                raise typer.Exit(1) from e
        if interval_hours <= 0:
            break
        console.print(f"[dim]Sleeping {interval_hours}h - Ctrl+C to stop.[/dim]")
        time.sleep(interval_hours * 3600)


@app.command()
def init():
    """Create .env from the template and list where to get FREE API keys."""
    from .config import PACKAGE_DIR

    env = Path.cwd() / ".env"
    example = PROJECT_ROOT / ".env.example"
    if not example.exists():
        example = PACKAGE_DIR / "config" / "env.example"
    if not env.exists() and example.exists():
        shutil.copy(example, env)
        console.print(f"[green]Created {env}[/green] - paste your keys there.")
    else:
        console.print(f"[dim].env already exists at {env}[/dim]" if env.exists() else "[yellow].env.example not found[/yellow]")
    from .llm import Registry

    reg = Registry.load(load_settings().models_file)
    t = Table(title="Free LLM providers (add any - the router uses all of them)")
    for col in ("Provider", "Env var", "Free tier", "Get a key"):
        t.add_column(col, overflow="fold")
    for p in reg.providers.values():
        t.add_row(p.name, " | ".join(p.api_key_env) or "(local)", p.notes, p.signup)
    console.print(t)
    console.print("Search works with no keys (DuckDuckGo, Parallel MCP, Google News, HN, Wikipedia, GitHub). "
                  "Optional keys: TAVILY_API_KEY, EXA_API_KEY, JINA_API_KEY, SERPER_API_KEY, BRAVE_API_KEY.")


@app.command()
def mcp(transport: str = typer.Option("stdio", help="stdio | http"), port: int = 8770):
    """Serve Resurge as an MCP server (tools: research, discover, reports, knowledge base)."""
    from .mcp_server import serve

    serve(transport=transport, port=port)


@app.command()
def ui(host: str = "127.0.0.1", port: int = 8765):
    """Launch the local web dashboard."""
    try:
        import uvicorn
    except ImportError as e:
        console.print("[red]Install the web extra: pip install 'resurge[web]'[/red]")
        raise typer.Exit(1) from e
    from .web.app import create_app

    console.print(f"[magenta]Resurge dashboard[/magenta]: http://{host}:{port}")
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


@app.command()
def admin(email: str = typer.Argument(..., help="The admin's email address (also listed in RESURGE_ADMIN_EMAILS)"),
          password_stdin: bool = typer.Option(False, "--password-stdin", help="Read the password from standard input")):
    """Create the admin account for the hosted web app (or take over that inbox's account and reset its password).

    Admins are only ever made here, on the server: the web sign-up refuses addresses listed in RESURGE_ADMIN_EMAILS,
    so nobody can claim the admin address before you do. In Docker: docker compose exec resurge resurge admin EMAIL"""
    from .web.accounts import AccountError, Accounts
    from .web.hosted import HostedConfig

    s = _settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    cfg = HostedConfig()
    password = (sys.stdin.readline().rstrip("\n") if password_stdin
                else typer.prompt("Password (10+ characters)", hide_input=True, confirmation_prompt=True))
    try:
        user = Accounts(s.data_dir / "accounts.db", cfg.admin_emails).make_admin(email, password)
    except AccountError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if user.is_admin:
        console.print(f"[green]Admin account ready:[/green] {user.email}. Log in on the website.")
    else:
        console.print(f"[yellow]Saved {user.email}, but it is not in RESURGE_ADMIN_EMAILS yet: add it there (and "
                      "restart) to give it admin rights.[/yellow]")


@app.command()
def serve(host: str = typer.Option("0.0.0.0", help="Interface to listen on"),
          port: int = typer.Option(int(os.getenv("PORT", "8000")), help="Port (the PORT env var is honoured)")):
    """Run the hosted multi-user web app: sign up, log in, type a company (for Docker and servers)."""
    try:
        import uvicorn
    except ImportError as e:
        console.print("[red]Install the web extra: pip install 'resurge[web,rag]'[/red]")
        raise typer.Exit(1) from e
    from urllib.parse import urlparse

    from .web.hosted import create_hosted_app, default_frontend_dir

    site = Path(os.getenv("RESURGE_FRONTEND_DIR") or default_frontend_dir())
    if not (site / "index.html").is_file():
        console.print(f"[yellow]The website is not built yet ({site}). Build it once: "
                      "cd frontend && npm install && npm run build[/yellow]")
    trust_proxy = os.getenv("RESURGE_TRUST_PROXY", "0").strip().lower() in ("1", "true", "yes", "caddy", "cloudflare")
    console.print(f"[magenta]Resurge web app[/magenta]: http://{host}:{port}"
                  + (" (trusting proxy headers)" if trust_proxy else ""))
    public = os.getenv("RESURGE_PUBLIC_URL", "").strip()
    if public.startswith("http://") and urlparse(public).hostname not in ("localhost", "127.0.0.1", "::1"):
        console.print("[yellow]RESURGE_PUBLIC_URL starts with http://: passwords and session cookies would travel "
                      "unencrypted. Put HTTPS in front (Caddy) and use the https:// address.[/yellow]")
    # no "server: uvicorn" header: nothing that tells a visitor which server and version answer
    uvicorn.run(create_hosted_app(), host=host, port=port, log_level="info", proxy_headers=trust_proxy,
                forwarded_allow_ips="*" if trust_proxy else "127.0.0.1", server_header=False)


@app.command()
def purge(cache: bool = typer.Option(True, help="Cached pages, search results and LLM responses"),
          knowledge: bool = typer.Option(False, help="Verified-facts knowledge base"),
          learning: bool = typer.Option(False, help="Playbook lessons, model & source reputation, scores"),
          history: bool = typer.Option(False, help="Run registry, activity feed, telemetry, quota counters"),
          reports: bool = typer.Option(False, help="Generated deliverables under reports/"),
          yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation")):
    """Delete stored data (data minimisation / retention control)."""
    from .storage import Storage

    s = _settings()
    targets = [n for n, on in (("cache", cache), ("knowledge base", knowledge), ("learning state", learning),
                               ("history", history), ("reports", reports)) if on]
    if not targets:
        console.print("Nothing selected.")
        return
    if not yes and not typer.confirm(f"Permanently delete: {', '.join(targets)}?"):
        raise typer.Exit(1)
    st = Storage(s.db_path)
    if cache:
        console.print(f"cache: {st.clear_cache()} entries deleted")
    if knowledge:
        console.print(f"knowledge base: {st.clear_knowledge()} facts deleted")
    if learning:
        st.reset_learning()
        console.print("learning state reset")
    if history:
        st.clear_history()
        console.print("history cleared")
    if reports and s.reports_dir and s.reports_dir.exists():
        shutil.rmtree(s.reports_dir)
        console.print(f"reports deleted: {s.reports_dir}")


@app.command()
def demo(out: Path = typer.Option(Path("reports/demo"), help="Where to write the sample deliverables")):
    """Render a fictional sample engagement into every deliverable (no keys, no network)."""
    from . import outputs
    from .sample_report import make_sample_report

    arts = outputs.publish_all(make_sample_report(), out)
    console.print(Panel.fit("Sample deliverables for a fictional engagement (Nimbus Notes vs Atlas Docs, "
                            "Quill Workspace, Sprout Wiki) - every company and number is invented.",
                            border_style="magenta"))
    for k, v in arts.items():
        console.print(f"[cyan]{k}[/cyan]: {v}")


@app.command()
def version():
    """Print version."""
    console.print(f"resurge {__version__}")


@app.command(name="report-json", hidden=True)
def report_json(run_id: str):
    """Print a run's report.json path (for scripting)."""
    from .storage import Storage

    r = Storage(_settings().db_path).get_run(run_id)
    console.print(json.dumps({"report_dir": (r or {}).get("report_dir")}))


if __name__ == "__main__":
    app()
