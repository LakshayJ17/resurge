"""Local web dashboard (FastAPI). `resurge ui` -> http://127.0.0.1:8765

Endpoints
  GET  /                         dashboard (single self-contained page)
  GET  /api/runs                 engagements
  POST /api/runs                 start an engagement (runs in the background)
  GET  /api/runs/{id}            run detail (+ report summary when done)
  GET  /api/runs/{id}/events     activity feed (poll with ?after=<event id>)
  GET  /api/org                  org chart + current model routing
  GET  /api/learn                playbook, model leaderboard, source reputation, quality trend
  GET  /api/providers            provider/key status (no network calls)
  GET  /api/runs/{id}/chat/status  RAG index status of a run {indexed, chunks, backend}
  POST /api/runs/{id}/chat       ask a question about a finished run (RAG; indexes lazily on first question)
  GET  /files/{id}/{name}        deliverables (report.html, report.pdf, report.pptx, ...)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from ..agents.org import ORG
from ..budget import Budget
from ..config import load_settings
from ..llm import LLMGateway, Registry
from ..schemas import Depth, ResearchBrief, ResearchReport
from ..storage import Storage
from ..text import slugify

STATIC = Path(__file__).parent / "static"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def report_csp() -> str:
    """Content-Security-Policy for report pages served next to an API: only the two scripts Resurge itself writes
    (the Ask page and the print button) may run, nothing may be framed, posted or loaded from elsewhere."""
    import base64
    import hashlib

    from ..outputs.html_report import SCRIPT_HASH
    from ..outputs.site import ASK_SCRIPT

    ask = "sha256-" + base64.b64encode(hashlib.sha256(ASK_SCRIPT.encode()).digest()).decode()
    return ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; connect-src 'self'; "
            f"script-src '{ask}' '{SCRIPT_HASH.strip(chr(39))}'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
log = logging.getLogger(__name__)


class RunRequest(BaseModel):
    target: str
    competitors: list[str] = []
    industry: str | None = None
    focus_areas: list[str] = []
    questions: list[str] = []
    depth: Depth = Depth.quick
    max_competitors: int = 4
    local: bool = False
    private: bool = False


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=500)
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be empty")
        return v


def create_app() -> FastAPI:
    settings = load_settings()
    settings.ensure_dirs()
    storage = Storage(settings.db_path)
    app = FastAPI(title="Resurge", version="0.1.0")
    tasks: dict[str, asyncio.Task] = {}
    allowed_hosts = LOOPBACK | {h.strip().lower() for h in os.getenv("RESURGE_ALLOWED_HOSTS", "").split(",") if h.strip()}
    csp = report_csp()

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """This dashboard has no log-in, so it must only ever answer the person at this computer:
        * Host must be a local name: a web page that points its own domain at 127.0.0.1 (DNS rebinding) is refused;
        * other websites may not POST here (Origin must be this dashboard) nor make the browser load its API or
          files in the background (Sec-Fetch-Site: cross-site), e.g. an <img> that triggers model discovery."""
        if _host_name(request.headers.get("host", "")) not in allowed_hosts:
            return JSONResponse({"detail": "unknown host"}, status_code=421)
        if request.headers.get("sec-fetch-site") == "cross-site" and request.url.path != "/":
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin is not None and urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (STATIC / "index.html").read_text(encoding="utf-8")

    @app.get("/api/runs")
    def runs(limit: int = 30):
        out = []
        for r in storage.list_runs(limit):
            m = r.get("metrics") or {}
            out.append({"id": r["id"], "target": r.get("target"), "status": r.get("status"),
                        "created_at": r.get("created_at"), "quality": r.get("quality"),
                        "facts": m.get("facts_usable"), "llm_calls": m.get("llm_calls"), "error": r.get("error"),
                        "running": r["id"] in tasks and not tasks[r["id"]].done()})
        return out

    @app.post("/api/runs")
    async def start(req: RunRequest):
        from ..pipeline.orchestrator import ResurgeCompany

        s = load_settings()
        s.local_only, s.private_mode = req.local, req.private
        from ..pipeline.orchestrator import new_run_id

        run_id = new_run_id(req.target)
        from ..mcp_server import untrusted_brief

        try:
            brief = untrusted_brief(target=req.target, competitors=[c for c in req.competitors if c.strip()],
                                    industry=req.industry or None, focus_areas=req.focus_areas, questions=req.questions,
                                    depth=req.depth, max_competitors=req.max_competitors)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        company = ResurgeCompany(s, storage)

        async def job():
            from ..system import keep_awake, release

            awake = keep_awake(tie_to_process=False)  # held only while this engagement runs
            try:
                await company.run(brief, run_id=run_id)
            except Exception as e:  # noqa: BLE001 - recorded in the runs table by the orchestrator
                storage.update_run(run_id, status="failed", error=str(e)[:500])
            finally:
                release(awake)
        tasks[run_id] = asyncio.create_task(job())
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        r = storage.get_run(run_id)
        if not r:
            raise HTTPException(404, "unknown run")
        out = {"run": r, "files": [], "summary": None}
        if r.get("report_dir") and Path(r["report_dir"]).exists():
            d = Path(r["report_dir"])
            out["files"] = sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file())
            rj = d / "report.json"
            if rj.exists():
                rep = ResearchReport.model_validate_json(rj.read_text())
                out["summary"] = {
                    "headline": rep.executive_summary.headline, "summary": rep.executive_summary.summary,
                    "findings": [k.text for k in rep.executive_summary.key_findings],
                    "threats": rep.executive_summary.top_threats, "opportunities": rep.executive_summary.top_opportunities,
                    "competitors": [c.name for c in rep.competitors], "metrics": rep.metrics.model_dump(mode="json"),
                    "risks": [{"id": x.id, "title": x.title, "score": x.score, "category": x.category} for x in rep.risks[:8]],
                    "changes": rep.changes_since_last_run[:10],
                }
        return out

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0):
        return storage.get_events(run_id, after_id=after, limit=400)

    @app.get("/api/org")
    async def org():
        reg = Registry.load(settings.models_file)
        await reg.discover(settings, cache=storage)
        gw = LLMGateway(reg, storage, settings, budget=Budget())
        rows = []
        for p in ORG.values():
            route = [m.id for m in gw.candidates(p.role, 2000)[:3]] if p.role else []
            rows.append({"department": p.department, "title": p.title, "mission": p.mission, "role": p.role,
                         "routes": route})
        await gw.aclose()
        return rows

    @app.get("/api/learn")
    def learn():
        return {"lessons": storage.all_lessons(), "models": storage.get_model_stats(),
                "sources": [{**r, "reliability": round(storage.source_reliability(r["domain"], 0.5), 3)}
                            for r in storage.top_sources(20)],
                "quality": storage.score_history("quality_score", 30)}

    @app.get("/api/providers")
    def providers():
        reg = Registry.load(settings.models_file)
        return reg.summary(settings)

    # ------------------------------------------------------------ RAG chat
    def _known_run(run_id: str) -> dict:
        r = storage.get_run(run_id)
        if not r:
            raise HTTPException(404, "unknown run")
        return r

    def _same_origin(request: Request) -> None:
        """Browsers always send Origin on POST: refuse other sites spending the user's free quota (CSRF)."""
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            raise HTTPException(403, "cross-origin request refused")

    @app.get("/api/runs/{run_id}/chat/status")
    def chat_status(run_id: str):
        from .. import rag

        _known_run(run_id)
        try:
            st = rag.status(settings, run_id)
        except rag.RunNotFound as e:
            raise HTTPException(404, "unknown run") from e
        if st["backend"] == "unavailable":
            st = {**st, "hint": f"RAG chat is not available: install it with {rag.INSTALL_HINT}"
                  if not rag.available() else "The vector store is busy or unreachable; try again shortly."}
        return JSONResponse(st, headers={"Cache-Control": "no-store"})

    @app.post("/api/runs/{run_id}/chat")
    async def chat(run_id: str, req: ChatRequest, request: Request):
        from .. import rag

        _same_origin(request)
        _known_run(run_id)
        from ..input_guard import INJECTION_REPLY, screen_chat

        ok, history = screen_chat(req.question, [t.model_dump() for t in req.history])
        if not ok:
            return JSONResponse({"answer": INJECTION_REPLY, "citations": [], "confidence": "low", "followups": [],
                                 "grounded": False, "backend": "screen"}, headers={"Cache-Control": "no-store"})
        if not rag.available():
            raise HTTPException(503, f"RAG chat is not installed: {rag.INSTALL_HINT}")
        try:
            out = await rag.ask(settings, run_id, req.question, history)
        except rag.RunNotFound as e:
            raise HTTPException(409, "this run has no finished report to chat with yet") from e
        except rag.RagUnavailable as e:
            raise HTTPException(503, str(e)) from e
        except Exception as e:  # noqa: BLE001 - provider errors may carry upstream text: never echo them
            log.warning("chat failed for run %s: %s", run_id, type(e).__name__)
            raise HTTPException(503, "No free model could answer right now - please try again in a minute.") from e
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.get("/files/{run_id}/{name:path}")
    def files(run_id: str, name: str):
        r = storage.get_run(run_id)
        if not r or not r.get("report_dir"):
            raise HTTPException(404)
        base = Path(r["report_dir"]).resolve()
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file():
            raise HTTPException(404)
        resp = FileResponse(path)
        if path.suffix.lower() in (".html", ".htm"):  # report text comes from the web: only Resurge's scripts run
            resp.headers["Content-Security-Policy"] = csp
        return resp

    return app
