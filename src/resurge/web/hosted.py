"""Hosted multi-user web app: sign up, log in, type a company (or its website), get a report. `resurge serve`

The browser app is a Next.js static export (``frontend/``) served by this process from the same origin, so the
session cookie, the Origin checks and the Content-Security-Policy cover it. Everything else is JSON under /api.

Pages (static, rendered in the browser)       API (same origin, session cookie, JSON)
  /            what Resurge is, how it works    GET  /api/session                  who is logged in, limits, options
  /signup      create an account                POST /api/signup  /api/login  /api/logout
  /login       log in                           POST /api/account/password | logout-everywhere | delete
  /dashboard   new analysis, your analyses      GET  /api/analyses                 your analyses
  /analysis    progress, then the report        POST /api/analyses                 queue an analysis
  /account     password, sessions, delete       GET  /api/analyses/{id}            progress, summary, downloads
  /admin       usage, accounts, events          GET  /api/admin   POST /api/admin/users/{id}/{action}
  /privacy     what is stored                   GET  /api/runs/{id}/chat/status   POST /api/runs/{id}/chat
                                                GET  /a/{id}/{path}                report files (owner or admin only)
                                                GET  /healthz                      liveness for the host

Open sign-up shares one set of free API keys between everyone, so the app enforces daily analysis limits per
account, per IP and site-wide, a bounded queue with a fixed number of concurrent runs, and a daily chat limit.

Log-in security: scrypt hashes, per-account lockout with growing delays (persisted), per-IP and per-email throttling,
identical responses and timing for unknown accounts, optional breached-password check (k-anonymity, the password
never leaves the server), session rotation on log-in, 3-day idle and 14-day absolute session expiry, ``__Host-``
cookies over HTTPS, "log out everywhere", password change revoking every session, Origin checks on every POST,
honeypot and optional Cloudflare Turnstile on sign-up, and an audit log.

Prompt-injection safety for user input: company names, competitors and questions are length- and shape-checked and
scanned (resurge.security) before they reach any model, and questions are passed to agents as a fenced DATA block.
A web address typed as the company keeps only its host name. Chat questions and the client-supplied chat history are
scanned too; instruction-like input is refused without a model call, and answers stay grounded in cited research.

Guardrails (resurge.guardrails): after the cheap checks and limits, each research request and chat question is
classified by Llama Prompt Guard 2 (jailbreak/injection) and gpt-oss-safeguard-20b against Resurge's acceptable-use
policy (harmful help, espionage, private people, fake reviews). Chat answers and reports pass output checks (prompt
leaks via a canary token, secrets, personal data). Accounts with ``guard_strikes`` blocked attempts in 24 hours are
paused for the rest of that window.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from .. import guardrails as G
from ..config import load_settings
from ..input_guard import INJECTION_REPLY, InputRejected, screen_brief_inputs, screen_chat, website_input
from ..schemas import Depth, ResearchBrief, ResearchReport
from ..storage import Storage
from .accounts import AccountError, Accounts, RateLimiter, User, start_of_day
from .app import ChatRequest
from .progress import PHASES, depth_of, summarize

log = logging.getLogger(__name__)
COOKIE = "resurge_session"
SECURE_COOKIE = "__Host-resurge_session"  # browsers only accept it over HTTPS, host-only, path=/
MAX_BODY = 64 * 1024
# Blocked attempts that count toward a 24-hour pause (shape mistakes such as "too many competitors" do not)
STRIKE_EVENTS = ("input_blocked", "chat_blocked", "guardrail_blocked")
PAUSED_MSG = ("Your account is paused for 24 hours because several requests were blocked by the safety checks. "
              "If this is a mistake, contact the site owner.")
STATUS_TEXT = {"queued": "Waiting in line", "running": "Researching", "done": "Ready", "failed": "Did not finish"}
DOWNLOADS = {"report.pdf": "Full report (PDF)", "report.pptx": "Slide deck"}
# What a web run produces: the report page, the PDF and the slides, plus the data the app itself reads (report.json,
# the Ask page). No Markdown: nobody downloads it from the web app.
HOSTED_FORMATS = {"charts", "html", "pdf", "pptx", "json", "site"}
NO_STORE = {"Cache-Control": "no-store"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_set(name: str) -> set[str]:
    return {e.strip().lower() for e in os.getenv(name, "").split(",") if e.strip()}


@dataclass
class HostedConfig:
    signup_open: bool = field(default_factory=lambda: os.getenv("RESURGE_SIGNUP_OPEN", "1") != "0")
    admin_emails: set[str] = field(default_factory=lambda: _env_set("RESURGE_ADMIN_EMAILS"))
    runs_per_user_day: int = field(default_factory=lambda: _env_int("RESURGE_RUNS_PER_USER_DAY", 2))
    runs_per_ip_day: int = field(default_factory=lambda: _env_int("RESURGE_RUNS_PER_IP_DAY", 4))
    runs_total_day: int = field(default_factory=lambda: _env_int("RESURGE_RUNS_PER_DAY", 20))
    max_concurrent: int = field(default_factory=lambda: max(1, _env_int("RESURGE_MAX_CONCURRENT_RUNS", 1)))
    max_queue: int = field(default_factory=lambda: _env_int("RESURGE_MAX_QUEUE", 15))
    chat_per_user_day: int = field(default_factory=lambda: _env_int("RESURGE_CHAT_PER_USER_DAY", 40))
    allowed_depths: list[str] = field(default_factory=lambda: [d.strip() for d in
                                                               os.getenv("RESURGE_ALLOWED_DEPTHS", "quick,standard").split(",")
                                                               if d.strip() in {"quick", "standard", "deep"}] or ["quick"])
    turnstile_site_key: str = field(default_factory=lambda: os.getenv("TURNSTILE_SITE_KEY", "").strip())
    turnstile_secret: str = field(default_factory=lambda: os.getenv("TURNSTILE_SECRET_KEY", "").strip())
    public_url: str = field(default_factory=lambda: os.getenv("RESURGE_PUBLIC_URL", "").strip().rstrip("/"))
    trust_proxy: bool = field(default_factory=lambda: os.getenv("RESURGE_TRUST_PROXY", "0") == "1")
    private_mode: bool = field(default_factory=lambda: os.getenv("RESURGE_PRIVATE", "0") == "1")
    pwned_check: bool = field(default_factory=lambda: os.getenv("RESURGE_PWNED_CHECK", "1") != "0")
    guard_strikes: int = field(default_factory=lambda: _env_int("RESURGE_GUARD_STRIKES", 5))
    # the built Next.js site (frontend/out); empty = the repository's frontend/out
    frontend_dir: str = field(default_factory=lambda: os.getenv("RESURGE_FRONTEND_DIR", "").strip())
    # extra origins allowed to POST, for the Next.js dev server only (e.g. http://localhost:3000)
    extra_origins: set[str] = field(default_factory=lambda: _env_set("RESURGE_ALLOWED_ORIGINS"))

    @property
    def turnstile(self) -> bool:
        return bool(self.turnstile_site_key and self.turnstile_secret)


def _sha(script: str | bytes) -> str:
    data = script.encode() if isinstance(script, str) else script
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


async def pwned_count(password: str, timeout: float = 3.0) -> int:
    """Times the password appears in known breaches (Have I Been Pwned, k-anonymity: only the first 5 hex chars of its
    SHA-1 leave the server). Fails open (0) when the service is unreachable."""
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()  # noqa: S324 - required by the API
    prefix, suffix = digest[:5], digest[5:]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"https://api.pwnedpasswords.com/range/{prefix}", headers={"Add-Padding": "true",
                                                                                         "User-Agent": "Resurge"})
        if r.status_code != 200:
            return 0
        for line in r.text.splitlines():
            h, _, n = line.partition(":")
            if h.strip() == suffix:
                return int(n.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0
    return 0


# --------------------------------------------------------------------------- request bodies
# Generous caps (the real limits, with friendly messages, are in input_guard and accounts); the 64 KB body limit
# in the middleware applies first.
class SignupBody(BaseModel):
    email: str = Field("", max_length=320)
    password: str = Field("", max_length=1000)
    website: str = Field("", max_length=500)  # honeypot: the field is invisible to people
    turnstile: str = Field("", max_length=4096)


class LoginBody(BaseModel):
    email: str = Field("", max_length=320)
    password: str = Field("", max_length=1000)


class PasswordBody(BaseModel):
    current: str = Field("", max_length=1000)
    new: str = Field("", max_length=1000)


class DeleteBody(BaseModel):
    password: str = Field("", max_length=1000)
    confirm: str = Field("", max_length=50)


class AnalysisBody(BaseModel):
    company: str = Field("", max_length=400)
    competitors: str = Field("", max_length=1000)
    questions: str = Field("", max_length=2000)
    depth: str = Field("quick", max_length=20)


# --------------------------------------------------------------------------- static frontend
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.S | re.I)


def default_frontend_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "frontend" / "out"


class Frontend:
    """Serves the Next.js static export. Each HTML page gets a CSP that allows exactly its own inline scripts (by
    SHA-256 hash) and same-origin files, so the strict policy of the old server-rendered pages still holds."""

    def __init__(self, root: Path, turnstile: bool):
        self.root = root.resolve()
        self.turnstile = turnstile
        self._csp: dict[Path, tuple[float, str]] = {}

    @property
    def built(self) -> bool:
        return (self.root / "index.html").is_file()

    def resolve(self, path: str) -> Path | None:
        path = path.strip("/")
        if "\x00" in path or any(part in ("..", "") for part in path.split("/")[:-1]):
            return None
        names = ["index.html"] if not path else [path, f"{path}.html", f"{path}/index.html"]
        for name in names:
            p = (self.root / name).resolve()
            if (p == self.root or self.root in p.parents) and p.is_file():
                return p
        return None

    def csp(self, page: Path) -> str:
        mtime = page.stat().st_mtime
        hit = self._csp.get(page)
        if hit and hit[0] == mtime:
            return hit[1]
        hashes = sorted({_sha(m.group(1)) for m in _INLINE_SCRIPT.finditer(page.read_bytes()) if m.group(1).strip()})
        script = ["'self'", *(f"'{h}'" for h in hashes)]
        policy = ["default-src 'self'", "img-src 'self' data:", "style-src 'self' 'unsafe-inline'", "font-src 'self'",
                  "connect-src 'self'", "form-action 'self'", "frame-ancestors 'none'", "base-uri 'none'",
                  "object-src 'none'"]
        if self.turnstile:
            script.append("https://challenges.cloudflare.com")
            policy.append("frame-src https://challenges.cloudflare.com")
        policy.append("script-src " + " ".join(script))
        value = "; ".join(policy)
        self._csp[page] = (mtime, value)
        return value

    def response(self, page: Path, status_code: int = 200) -> Response:
        resp = FileResponse(page, status_code=status_code)
        if page.suffix == ".html":
            resp.headers["Content-Security-Policy"] = self.csp(page)
        if "/_next/static/" in page.as_posix():
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # content-hashed file names
        else:
            resp.headers["Cache-Control"] = "private, no-store"  # a new deploy shows up at once
        return resp

    def not_found(self, status_code: int = 404) -> Response:
        page = self.resolve("404")
        if page:
            return self.response(page, status_code)
        return HTMLResponse("<!doctype html><title>Not found</title><p>Not found. <a href='/'>Back to Resurge</a></p>",
                            status_code=status_code, headers={"Cache-Control": "private, no-store"})


NOT_BUILT = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Resurge</title>
<body style="font:16px/1.6 system-ui;max-width:40rem;margin:4rem auto;padding:0 1rem">
<h1>The website has not been built yet</h1>
<p>Build it once, then reload this page:</p>
<pre>cd frontend
npm install
npm run build</pre>
<p>The API is running; <code>/healthz</code> reports its status.</p></body></html>"""


def create_hosted_app(config: HostedConfig | None = None, settings=None) -> FastAPI:
    cfg = config or HostedConfig()
    settings = settings or load_settings()
    if not os.getenv("RESURGE_GUARDRAILS"):
        settings.guardrails = "strict"  # strangers' requests: refuse when the policy check cannot run
    settings.ensure_dirs()
    storage = Storage(settings.db_path)
    accounts = Accounts(settings.data_dir / "accounts.db", cfg.admin_emails)
    limiter = RateLimiter()
    frontend = Frontend(Path(cfg.frontend_dir).expanduser() if cfg.frontend_dir else default_frontend_dir(),
                        cfg.turnstile)
    queue: asyncio.Queue[str] = asyncio.Queue()
    workers: list[asyncio.Task] = []
    guard_state: dict = {"llm": None, "tried": 0.0}
    guard_lock = asyncio.Lock()

    from ..outputs.html_report import SCRIPT_HASH as PRINT_SCRIPT_HASH
    from ..outputs.site import ASK_SCRIPT
    report_csp = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; connect-src 'self'; "
                  f"script-src '{_sha(ASK_SCRIPT)}' '{PRINT_SCRIPT_HASH.removeprefix(chr(39)).removesuffix(chr(39))}'; "
                  "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    # ---------------------------------------------------------------- engine
    async def worker() -> None:
        from ..pipeline.orchestrator import ResurgeCompany

        while True:
            run_id = await queue.get()
            try:
                job = accounts.job(run_id)
                if not job or job["status"] != "queued":
                    continue
                accounts.set_job(run_id, status="running", started_at=time.time())
                brief = ResearchBrief.model_validate_json(job["brief"])
                # A copy of the app's own settings (not a fresh load_settings()): runs must land in the data and
                # reports folders this app serves from, and the per-run private flag must not leak into `settings`.
                s = replace(settings)
                s.private_mode = s.private_mode or cfg.private_mode
                try:
                    await ResurgeCompany(s, storage).run(brief, run_id=run_id, formats=set(HOSTED_FORMATS))
                    row = storage.get_run(run_id) or {}
                    ok = row.get("status") == "done"
                    accounts.set_job(run_id, status="done" if ok else "failed", finished_at=time.time(),
                                     error=None if ok else "The research could not finish. Please try again later.")
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - details go to the server log, never to the user
                    log.warning("analysis %s failed: %s: %s", run_id, type(e).__name__, str(e)[:300])
                    msg = ("No free AI model is available right now. Please try again in an hour."
                           if "provider" in str(e).lower() or "model" in str(e).lower()
                           else "The research could not finish. Please try again later.")
                    accounts.set_job(run_id, status="failed", finished_at=time.time(), error=msg)
            finally:
                queue.task_done()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        accounts.purge_expired_sessions()
        for job in accounts.recover_after_restart():
            queue.put_nowait(job["run_id"])
        for _ in range(cfg.max_concurrent):
            workers.append(asyncio.create_task(worker()))
        if not frontend.built:
            log.warning("website not built: run `npm install && npm run build` in frontend/ (looked in %s)",
                        frontend.root)
        try:
            yield
        finally:
            for w in workers:
                w.cancel()
            if guard_state["llm"] is not None:
                await guard_state["llm"].aclose()

    app = FastAPI(title="Resurge", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.accounts, app.state.queue, app.state.config, app.state.frontend = accounts, queue, cfg, frontend

    # --------------------------------------------------------------- helpers
    def client_ip(request: Request) -> str:
        if cfg.trust_proxy:
            ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
            if ip.strip():
                return ip.strip()[:64]
        return (request.client.host if request.client else "unknown")[:64]

    def secure_cookies(request: Request) -> bool:
        return cfg.public_url.startswith("https://") or request.url.scheme == "https" or (
            cfg.trust_proxy and request.headers.get("x-forwarded-proto") == "https")

    def session_token(request: Request) -> str | None:
        return request.cookies.get(SECURE_COOKIE) or request.cookies.get(COOKIE)

    def current_user(request: Request) -> User | None:
        return accounts.session_user(session_token(request))

    def require_user(request: Request) -> User:
        user = current_user(request)
        if not user:
            raise HTTPException(401, "Please log in.")
        return user

    def same_origin(request: Request) -> None:
        """Every state-changing request must come from this site (CSRF defence on top of SameSite cookies)."""
        source = request.headers.get("origin") or request.headers.get("referer")
        if not source:
            raise HTTPException(403, "Request refused: missing origin.")
        allowed = {request.headers.get("host", "")}
        if cfg.public_url:
            allowed.add(urlparse(cfg.public_url).netloc)
        allowed |= {urlparse(o).netloc for o in cfg.extra_origins}
        if urlparse(source).netloc not in allowed:
            raise HTTPException(403, "Request refused: it did not come from this site.")

    def owned_job(user: User, run_id: str) -> dict:
        job = accounts.job(run_id)
        if not job or (job["user_id"] != user.id and not user.is_admin):
            raise HTTPException(404, "Not found.")
        return job

    def reply(data: dict | None = None, status_code: int = 200) -> JSONResponse:
        return JSONResponse(data if data is not None else {"ok": True}, status_code=status_code, headers=NO_STORE)

    def fail(message: str, status_code: int = 400, **extra) -> JSONResponse:
        return JSONResponse({"detail": message, **extra}, status_code=status_code, headers=NO_STORE)

    def set_session(resp: Response, request: Request, user_id: int) -> None:
        old = session_token(request)
        if old:  # rotate: never keep a token that existed before log-in (session fixation)
            accounts.end_session(old)
        secure = secure_cookies(request)
        resp.set_cookie(SECURE_COOKIE if secure else COOKIE, accounts.create_session(user_id), max_age=14 * 86400,
                        httponly=True, samesite="lax", secure=secure, path="/")

    def clear_session(resp: Response) -> None:
        resp.delete_cookie(COOKIE, path="/")
        resp.delete_cookie(SECURE_COOKIE, path="/", secure=True)

    async def guard_llm():
        """Shared gateway for input guardrails, created on first use (retried every 5 minutes if no provider)."""
        if settings.guardrails == "off":
            return None
        async with guard_lock:
            if guard_state["llm"] is None and time.time() - guard_state["tried"] > 300:
                guard_state["tried"] = time.time()
                try:
                    guard_state["llm"] = await G.make_gateway(settings, "guardrails")
                except Exception as e:  # noqa: BLE001 - the rule layer still runs
                    log.warning("guardrail gateway unavailable: %s", type(e).__name__)
            return guard_state["llm"]

    def paused(user: User) -> bool:
        return (not user.is_admin and cfg.guard_strikes > 0
                and accounts.count_events(user.id, STRIKE_EVENTS, time.time() - 86400) >= cfg.guard_strikes)

    def remaining_today(user: User) -> int | None:
        if user.is_admin:
            return None  # no limit
        return max(0, cfg.runs_per_user_day - accounts.jobs_since(start_of_day(), user_id=user.id))

    def session_view(user: User | None) -> dict:
        conf = {"signup_open": cfg.signup_open, "turnstile_site_key": cfg.turnstile_site_key if cfg.turnstile else "",
                "depths": cfg.allowed_depths, "runs_per_user_day": cfg.runs_per_user_day,
                "chat_per_user_day": cfg.chat_per_user_day, "private_mode": cfg.private_mode}
        me = None
        if user:
            me = {"email": user.email, "is_admin": user.is_admin, "remaining": remaining_today(user),
                  "paused": paused(user)}
        return {"user": me, "config": conf}

    def status_text(job: dict) -> str:
        if job["status"] == "queued":
            pos = accounts.queue_position(job["run_id"])
            if pos <= 1:
                return "Next in line"
            suffix = "th" if 10 <= pos % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(pos % 10, "th")
            return f"{pos}{suffix} in line"
        return STATUS_TEXT.get(job["status"], job["status"])

    def job_view(job: dict) -> dict:
        return {"run_id": job["run_id"], "target": job["target"], "status": job["status"],
                "status_text": status_text(job), "created_at": job["created_at"],
                "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
                "error": job.get("error") if job["status"] == "failed" else None,
                "report_url": f"/a/{job['run_id']}/report.html" if job["status"] == "done" else None}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = MAX_BODY + 1
        if length > MAX_BODY:
            return JSONResponse({"detail": "Request too large."}, status_code=413)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if secure_cookies(request):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=NO_STORE)
        if exc.status_code == 401:  # a report link opened while logged out: log in, then come back
            return RedirectResponse("/login?next=" + quote(request.url.path, safe="/"), status_code=303)
        return frontend.not_found(exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def bad_body(request: Request, exc: RequestValidationError):
        return fail("Please check what you entered and try again.", 400)

    # ------------------------------------------------------------- session
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "queued": queue.qsize(), "website": frontend.built}

    @app.get("/api/session")
    def get_session(request: Request):
        return reply(session_view(current_user(request)))

    async def verify_turnstile(token: str, ip: str) -> bool:
        if not cfg.turnstile:
            return True
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                                      data={"secret": cfg.turnstile_secret, "response": token, "remoteip": ip})
            return bool(r.json().get("success"))
        except Exception:  # noqa: BLE001
            return False

    @app.post("/api/signup")
    async def signup(request: Request, body: SignupBody):
        same_origin(request)
        ip = client_ip(request)
        if not cfg.signup_open:
            return fail("Sign-up is closed. Ask the owner of this site for an account.", 403)
        if body.website:  # honeypot: humans never see this field
            accounts.audit("signup_bot", email=body.email, ip=ip, detail="honeypot")
            return fail("Sign-up failed. Please try again.")
        if not limiter.hit(f"signup:{ip}", 5, 3600) or accounts.signups_from_ip(ip, time.time() - 86400) >= 5:
            accounts.audit("signup_throttled", email=body.email, ip=ip)
            return fail("Too many sign-ups from your network. Try tomorrow.", 429)
        if not await verify_turnstile(body.turnstile, ip):
            return fail("Please complete the check that you are human.")
        if cfg.pwned_check and len(body.password) >= 10 and await pwned_count(body.password) > 0:
            return fail("That password has appeared in a known data breach. Please choose another.")
        try:
            user = accounts.create_user(body.email, body.password, ip)
        except AccountError as e:
            return fail(str(e))
        resp = reply(session_view(user), 201)
        set_session(resp, request, user.id)
        return resp

    @app.post("/api/login")
    async def login(request: Request, body: LoginBody):
        same_origin(request)
        ip = client_ip(request)
        key = body.email.strip().lower()[:120]
        if not limiter.hit(f"login-ip:{ip}", 20, 900) or not limiter.hit(f"login-email:{key}", 8, 900):
            accounts.audit("login_throttled", email=key, ip=ip)
            return fail("Too many attempts. Wait 15 minutes and try again.", 429)
        try:
            user = accounts.authenticate(body.email, body.password, ip)
        except AccountError as e:
            return fail(str(e))
        resp = reply(session_view(user))
        set_session(resp, request, user.id)
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        same_origin(request)
        user = current_user(request)
        accounts.end_session(session_token(request))
        if user:
            accounts.audit("logout", user_id=user.id, email=user.email, ip=client_ip(request))
        resp = reply()
        clear_session(resp)
        return resp

    # --------------------------------------------------------------- account
    @app.post("/api/account/password")
    async def change_password(request: Request, body: PasswordBody):
        same_origin(request)
        user = require_user(request)
        if not limiter.hit(f"pwchange:{user.id}", 5, 900):
            return fail("Too many attempts. Wait 15 minutes.", 429)
        if cfg.pwned_check and len(body.new) >= 10 and await pwned_count(body.new) > 0:
            return fail("That password has appeared in a known data breach. Please choose another.")
        try:
            accounts.change_password(user.id, body.current, body.new, client_ip(request))
        except AccountError as e:
            return fail(str(e))
        resp = reply({"ok": True, "message": "Password changed. You have been logged out everywhere else."})
        set_session(resp, request, user.id)  # this device stays logged in with a fresh token
        return resp

    @app.post("/api/account/logout-everywhere")
    async def logout_everywhere(request: Request):
        same_origin(request)
        user = require_user(request)
        accounts.revoke_sessions(user.id)
        accounts.audit("sessions_revoked", user_id=user.id, email=user.email, ip=client_ip(request))
        resp = reply()
        clear_session(resp)
        return resp

    @app.post("/api/account/delete")
    async def delete_account(request: Request, body: DeleteBody):
        same_origin(request)
        user = require_user(request)
        if body.confirm.strip().lower() != "delete":
            return fail("Type DELETE to confirm.")
        if not limiter.hit(f"delete:{user.id}", 5, 900):
            return fail("Too many attempts. Wait 15 minutes.", 429)
        try:
            run_ids = accounts.delete_user(user.id, body.password, client_ip(request))
        except AccountError as e:
            return fail(str(e))
        for run_id in run_ids:  # remove the reports too: deleting an account deletes its data
            row = storage.get_run(run_id)
            d = Path(row["report_dir"]).resolve() if row and row.get("report_dir") else None
            if d and Path(settings.reports_dir).resolve() in d.parents and d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        resp = reply({"ok": True, "message": "Your account, your analyses and their reports have been deleted."})
        clear_session(resp)
        return resp

    # -------------------------------------------------------------- analyses
    @app.get("/api/analyses")
    def list_analyses(request: Request):
        user = require_user(request)
        jobs = []
        for j in accounts.jobs_for(user.id):
            view = job_view(j)
            if j["status"] == "running":  # which of the seven steps it is on
                prog = summarize(storage.get_events(j["run_id"], after_id=0, limit=5000), "running")
                if prog["current"] is not None:
                    view["step"], view["steps"] = prog["current"] + 1, len(PHASES)
                    view["status_text"] = f"Step {prog['current'] + 1} of {len(PHASES)}: {PHASES[prog['current']][0]}"
            jobs.append(view)
        return reply({"jobs": jobs, "remaining": remaining_today(user)})

    @app.post("/api/analyses")
    async def new_analysis(request: Request, body: AnalysisBody):
        same_origin(request)
        user = require_user(request)
        ip = client_ip(request)
        if paused(user):
            return fail(PAUSED_MSG, 429)
        if body.depth not in cfg.allowed_depths:
            return fail("Choose one of the offered research depths.")
        try:
            site = website_input(body.company)  # "linear.app" -> ("Linear", "https://linear.app")
            name, comps, qs = screen_brief_inputs(site[0] if site else body.company, body.competitors, body.questions)
        except InputRejected as e:
            accounts.audit("input_blocked" if e.suspicious else "input_rejected", user_id=user.id, email=user.email,
                           ip=ip, detail=str(e)[:120])
            return fail(str(e))
        dup = accounts.active_duplicate(user.id, name)
        if dup:  # double click or reload: show the analysis that is already running
            return reply({"run_id": dup["run_id"], "existing": True})
        if not user.is_admin:
            today = start_of_day()
            if accounts.jobs_since(today) >= cfg.runs_total_day:
                return fail("Today's free research capacity is used up. Please come back tomorrow.", 429)
            if accounts.jobs_since(today, user_id=user.id) >= cfg.runs_per_user_day:
                return fail(f"You have used your {cfg.runs_per_user_day} analyses for today. More tomorrow.", 429)
            if accounts.jobs_since(today, ip=ip) >= cfg.runs_per_ip_day:
                return fail("Your network has reached today's analysis limit. More tomorrow.", 429)
            if len(accounts.queued_jobs()) >= cfg.max_queue:
                return fail("The queue is full right now. Please try again in an hour.", 429)
        # model guardrails run last, so requests refused by the cheap checks above cost no model calls
        verdict = await G.Guardrails(await guard_llm(), settings.guardrails).check(G.request_text(name, comps, qs))
        if not verdict.allowed:
            event = "guardrail_unavailable" if verdict.category == "UNAVAILABLE" else "guardrail_blocked"
            accounts.audit(event, user_id=user.id, email=user.email, ip=ip, detail=f"request {verdict.summary()}")
            return fail(verdict.message, 503 if verdict.category == "UNAVAILABLE" else 400)
        if verdict.degraded:
            accounts.audit("guardrail_degraded", user_id=user.id, email=user.email, ip=ip,
                           detail="request checked by: " + ", ".join(verdict.checked))
        from ..mcp_server import untrusted_brief
        from ..pipeline.orchestrator import new_run_id

        try:
            brief = untrusted_brief(target=name, target_url=site[1] if site else None, competitors=comps,
                                    questions=qs, depth=Depth(body.depth))
        except ValueError:
            return fail("Enter the name of a company, product or market.")
        run_id = new_run_id(brief.target)
        accounts.add_job(run_id, user.id, brief.target, brief.model_dump(mode="json"), ip)
        accounts.audit("analysis_started", user_id=user.id, email=user.email, ip=ip,
                       detail=brief.target + (f" ({brief.target_url})" if brief.target_url else ""))
        await queue.put(run_id)
        return reply({"run_id": run_id, "existing": False}, 201)

    @app.get("/api/analyses/{run_id}")
    def analysis(request: Request, run_id: str):
        user = require_user(request)
        job = owned_job(user, run_id)
        out = job_view(job)
        out["position"] = accounts.queue_position(run_id)
        depth = depth_of(job.get("brief"))
        prog = summarize(storage.get_events(run_id, after_id=0, limit=5000), job["status"], depth)
        if job["status"] == "running":
            current = prog["current"]
            out["status_text"] = prog["phases"][current]["step"] if current is not None else "Getting started"
        out["now"], out["depth"], out["typical"], out["slow_after_s"] = time.time(), depth, prog["typical"], prog["slow_after_s"]
        out["progress"] = {"phases": prog["phases"], "counts": prog["counts"]}
        out["events"] = prog["feed"][-60:]
        out["summary"], out["files"], out["missing"] = None, [], False
        if job["status"] == "done":
            row = storage.get_run(run_id) or {}
            d = Path(row["report_dir"]) if row.get("report_dir") else None
            if d and (d / "report.json").is_file():
                rep = ResearchReport.model_validate_json((d / "report.json").read_text())
                has_site = (d / "site" / "index.html").is_file()
                out["summary"] = {"headline": rep.executive_summary.headline, "summary": rep.executive_summary.summary,
                                  "competitors": [c.name for c in rep.competitors],
                                  "facts": rep.metrics.facts_usable, "quality": rep.metrics.quality_score}
                # the long report page is the report; runs made before it existed fall back to the old site
                out["report_url"] = (f"/a/{run_id}/report.html" if (d / "report.html").is_file()
                                     else f"/a/{run_id}/site/index.html" if has_site else None)
                out["ask_url"] = f"/a/{run_id}/site/ask.html" if has_site else None
                out["pdf_url"] = f"/a/{run_id}/report.pdf" if (d / "report.pdf").is_file() else None
                out["files"] = [{"label": label, "href": f"/a/{run_id}/{name}"} for name, label in DOWNLOADS.items()
                                if (d / name).is_file()]
            else:
                out["missing"], out["report_url"] = True, None
        return reply(out)

    @app.get("/a/{run_id}/{name:path}")
    def report_file(request: Request, run_id: str, name: str):
        user = require_user(request)
        owned_job(user, run_id)
        row = storage.get_run(run_id)
        if not row or not row.get("report_dir"):
            raise HTTPException(404, "Not found.")
        base = Path(row["report_dir"]).resolve()
        path = (base / name).resolve()
        if base not in path.parents or not path.is_file() or path.name == "report.json":
            raise HTTPException(404, "Not found.")
        resp = FileResponse(path)
        resp.headers["Cache-Control"] = "private, no-store"
        if path.suffix.lower() in (".pdf", ".pptx"):  # save with a readable name
            stem = re.sub(r"[^A-Za-z0-9]+", "-", (accounts.job(run_id) or {}).get("target", "report")).strip("-")[:60]
            resp.headers["Content-Disposition"] = (f'attachment; filename="{stem or "report"}-competitor-report'
                                                   f'{"" if path.stem == "report" else "-" + path.stem}{path.suffix}"')
        if path.suffix.lower() in (".html", ".htm"):  # report text comes from the web: second CSP layer
            resp.headers["Content-Security-Policy"] = report_csp
        return resp

    # ------------------------------------------------------------------ chat
    @app.get("/api/runs/{run_id}/chat/status")
    def chat_status(request: Request, run_id: str):
        from .. import rag

        user = require_user(request)
        owned_job(user, run_id)
        try:
            st = rag.status(settings, run_id)
        except Exception:  # noqa: BLE001
            st = {"indexed": False, "chunks": 0, "backend": "unavailable"}
        return reply(st)

    @app.post("/api/runs/{run_id}/chat")
    async def chat(request: Request, run_id: str, req: ChatRequest):
        from .. import rag

        same_origin(request)
        user = require_user(request)
        job = owned_job(user, run_id)
        if job["status"] != "done":
            raise HTTPException(409, "This analysis is not finished yet.")
        if paused(user):
            raise HTTPException(429, PAUSED_MSG)
        if not user.is_admin and accounts.usage_since(user.id, "chat", start_of_day()) >= cfg.chat_per_user_day:
            raise HTTPException(429, f"You have asked {cfg.chat_per_user_day} questions today. More tomorrow.")
        ok, history = screen_chat(req.question, [t.model_dump() for t in req.history])
        accounts.record_usage(user.id, "chat")
        if not ok:
            accounts.audit("chat_blocked", user_id=user.id, email=user.email, ip=client_ip(request), detail=run_id)
            return reply({"answer": INJECTION_REPLY, "citations": [], "confidence": "low", "followups": [],
                          "grounded": False, "backend": "screen"})
        if not rag.available():
            raise HTTPException(503, "Questions are not available on this server.")
        try:
            out = await rag.ask(settings, run_id, req.question, history)
        except Exception as e:  # noqa: BLE001 - never echo provider errors
            log.warning("chat failed for %s: %s", run_id, type(e).__name__)
            raise HTTPException(503, "No free model could answer right now. Please try again in a minute.") from e
        g = out.pop("guardrail", None)  # which layer fired stays server-side (no hints for someone probing it)
        if g:
            event = "guardrail_unavailable" if g.get("category") == "UNAVAILABLE" else "guardrail_blocked"
            accounts.audit(event, user_id=user.id, email=user.email, ip=client_ip(request),
                           detail=f"chat {g.get('category')} via {g.get('layer')} ({run_id})")
        return reply(out)

    # ----------------------------------------------------------------- admin
    @app.get("/api/admin")
    def admin(request: Request):
        user = require_user(request)
        if not user.is_admin:
            raise HTTPException(404, "Not found.")
        users = accounts.list_users()
        emails = {u["id"]: u["email"] for u in users}
        jobs = [{**job_view(j), "email": emails.get(j["user_id"], "(deleted)"), "error": j.get("error")}
                for j in accounts.all_jobs(100)]
        people = [{"id": u["id"], "email": u["email"], "is_admin": bool(u["is_admin"]), "created_at": u["created_at"],
                   "failed_logins": u.get("failed_logins") or 0, "disabled": bool(u["disabled"]),
                   "locked": (u.get("locked_until") or 0) > time.time(), "you": u["id"] == user.id} for u in users]
        events = [{"ts": e["ts"], "event": e["event"], "email": e.get("email") or "", "ip": e.get("ip") or "",
                   "detail": e.get("detail") or ""} for e in accounts.recent_audit(60)]
        stats = {"users": len(users), "today": accounts.jobs_since(start_of_day()),
                 "queued": len(accounts.queued_jobs()), "cap": cfg.runs_total_day}
        return reply({"stats": stats, "jobs": jobs, "users": people, "events": events})

    @app.post("/api/admin/users/{user_id}/{action}")
    async def admin_user(request: Request, user_id: int, action: str):
        same_origin(request)
        user = require_user(request)
        if not user.is_admin or action not in ("disable", "enable") or user_id == user.id:
            raise HTTPException(404, "Not found.")
        accounts.set_disabled(user_id, action == "disable", by=user.email)
        return reply()

    # -------------------------------------------------------------- website
    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def website(path: str):
        if path.startswith("api/") or path == "api":
            raise HTTPException(404, "Not found.")
        if not frontend.built:
            return HTMLResponse(NOT_BUILT, status_code=503, headers={"Cache-Control": "no-store"})
        page = frontend.resolve(path)
        return frontend.response(page) if page else frontend.not_found()

    return app


__all__ = ["HostedConfig", "InputRejected", "create_hosted_app", "pwned_count", "screen_brief_inputs", "screen_chat"]
