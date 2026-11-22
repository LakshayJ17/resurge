"""Accounts for the hosted web app: users, sessions, analysis ownership, the job queue, usage limits and an audit log.

Standard library only (no new dependencies in the security path):
* passwords: NFKC-normalised, scrypt (n=2**14, r=8, p=1, 64-byte key), random 16-byte salt, constant-time compare;
* brute force: per-account failure counter with growing lockouts (15, 30, then 60 minutes at most, so nobody can
  lock a victim out for long), stored in the database (survives restarts), on top of per-IP and per-email limits;
* sessions: 32-byte random tokens, only their SHA-256 stored; 14-day absolute and 3-day idle expiry; all sessions
  revoked on password change, account disable and "log out everywhere";
* one person, one account: plus-addressing (ana+1@) and Gmail dots count as the same email; throwaway email
  domains are refused;
* every security-relevant event lands in the ``audit`` table (no passwords, no tokens).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_DAYS = 14
SESSION_IDLE_DAYS = 3
LOCK_AFTER = 5  # failed log-ins before the first lockout
LOCK_BASE_MIN = 15  # first lockout; doubles with each further failure
LOCK_MAX_S = 3600  # capped at 1 hour, so an attacker cannot lock someone out for a day
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=64)

DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "sharklasers.com", "10minutemail.com",
    "10minutemail.net", "temp-mail.org", "tempmail.com", "tempmail.net", "yopmail.com", "yopmail.net", "trashmail.com",
    "getnada.com", "dispostable.com", "maildrop.cc", "mailnesia.com", "throwawaymail.com", "fakeinbox.com",
    "mintemail.com", "mohmal.com", "emailondeck.com", "tempr.email", "discard.email", "spamgourmet.com", "moakt.com",
})
COMMON_PASSWORDS = frozenset({
    "password12", "password123", "password1234", "1234567890", "12345678910", "qwertyuiop", "qwerty1234",
    "qwerty12345", "1q2w3e4r5t", "iloveyou12", "letmein123", "welcome123", "admin12345", "abc1234567", "passw0rd12",
    "football12", "baseball12", "sunshine12", "princess12", "dragon1234", "monkey1234", "trustno1!!", "superman12",
    "starwars12", "computer12", "whatever12", "changeme12", "resurge123", "competitor", "0987654321", "1111111111",
    "aaaaaaaaaa", "asdfghjkl1", "zxcvbnm123", "password!!", "p@ssw0rd12", "qazwsxedc1",
})

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  created_at REAL NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
  signup_ip TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS jobs(
  run_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, target TEXT NOT NULL, brief TEXT NOT NULL,
  status TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL, finished_at REAL, error TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id, created_at);
CREATE TABLE IF NOT EXISTS usage(
  user_id INTEGER NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_usage ON usage(user_id, kind, ts);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event TEXT NOT NULL, user_id INTEGER, email TEXT,
  ip TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit(user_id, ts);
"""
# columns added after the first release: (table, column, declaration)
MIGRATIONS = [
    ("users", "email_canonical", "TEXT"), ("users", "failed_logins", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "locked_until", "REAL NOT NULL DEFAULT 0"), ("users", "last_login", "REAL"),
    ("users", "password_changed", "REAL"), ("sessions", "last_seen", "REAL"),
]


class AccountError(ValueError):
    """A user-facing problem (bad email, wrong password, limit reached); the message is safe to show."""


def _norm_pw(password: str) -> bytes:
    return unicodedata.normalize("NFKC", password or "").encode("utf-8")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(_norm_pw(password), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, key_hex = stored.split("$")
        if algo != "scrypt":
            return False
        key = hashlib.scrypt(_norm_pw(password), salt=bytes.fromhex(salt_hex), **_SCRYPT)
        return hmac.compare_digest(key.hex(), key_hex)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password("timing-equaliser")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalise_email(email: str) -> str:
    return unicodedata.normalize("NFKC", email or "").strip().lower()


def canonical_email(email: str) -> str:
    """The identity behind an address: ana+test@x.com == ana@x.com; Gmail ignores dots and googlemail == gmail."""
    email = normalise_email(email)
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def blocked_email_domain(email: str) -> bool:
    extra = {d.strip().lower() for d in os.getenv("RESURGE_BLOCKED_EMAIL_DOMAINS", "").split(",") if d.strip()}
    domain = normalise_email(email).rsplit("@", 1)[-1]
    return any(domain == d or domain.endswith("." + d) for d in DISPOSABLE_DOMAINS | extra)


def check_password_strength(password: str, email: str = "") -> None:
    pw = unicodedata.normalize("NFKC", password or "")
    if len(pw) < 10:
        raise AccountError("Use at least 10 characters for your password.")
    if len(pw) > 200:
        raise AccountError("That password is too long.")
    low = pw.lower()
    if low in COMMON_PASSWORDS or len(set(low)) <= 2:
        raise AccountError("That password is too easy to guess. Choose another.")
    local = normalise_email(email).split("@", 1)[0]
    if len(local) >= 4 and local in low:
        raise AccountError("Don't use your email name in your password.")


@dataclass
class User:
    id: int
    email: str
    is_admin: bool


class Accounts:
    def __init__(self, path: Path | str, admin_emails: set[str] | None = None) -> None:
        self.path = str(path)
        self.admin_emails = {normalise_email(e) for e in (admin_emails or set()) if e.strip()}
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, col, decl in MIGRATIONS:
            cols = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_canonical ON users(email_canonical)")

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def _user(self, row: sqlite3.Row) -> User:
        return User(id=row["id"], email=row["email"], is_admin=bool(row["is_admin"]) or row["email"] in self.admin_emails)

    # ------------------------------------------------------------------ audit
    def audit(self, event: str, *, user_id: int | None = None, email: str = "", ip: str = "", detail: str = "") -> None:
        self._exec("INSERT INTO audit(ts,event,user_id,email,ip,detail) VALUES(?,?,?,?,?,?)",
                   (time.time(), event, user_id, normalise_email(email)[:254], ip[:64], detail[:300]))

    def count_events(self, user_id: int, events: tuple[str, ...], since: float) -> int:
        marks = ",".join("?" for _ in events)
        return int(self._one(f"SELECT COUNT(*) AS n FROM audit WHERE user_id=? AND ts>? AND event IN ({marks})",
                             (user_id, since, *events))["n"])

    def recent_audit(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------------ users
    def create_user(self, email: str, password: str, ip: str = "") -> User:
        email = normalise_email(email)
        if not EMAIL_RE.match(email):
            raise AccountError("Enter a valid email address.")
        if blocked_email_domain(email):
            raise AccountError("Please use a permanent email address.")
        check_password_strength(password, email)
        is_admin = 1 if email in self.admin_emails else 0
        try:
            cur = self._exec("INSERT INTO users(email,email_canonical,pw_hash,created_at,is_admin,signup_ip,password_changed) "
                             "VALUES(?,?,?,?,?,?,?)", (email, canonical_email(email), hash_password(password),
                                                       time.time(), is_admin, ip, time.time()))
        except sqlite3.IntegrityError as e:  # same email, or the same person behind a +alias / dotted Gmail
            self.audit("signup_duplicate", email=email, ip=ip)
            raise AccountError("We couldn't create an account with that email. If you already have one, log in.") from e
        self.audit("signup", user_id=int(cur.lastrowid), email=email, ip=ip)
        return User(id=int(cur.lastrowid), email=email, is_admin=bool(is_admin))

    def authenticate(self, email: str, password: str, ip: str = "") -> User:
        email = normalise_email(email)
        row = self._one("SELECT * FROM users WHERE email=?", (email,))
        if not row:
            verify_password(password, _DUMMY_HASH)  # same work whether or not the account exists
            self.audit("login_failed", email=email, ip=ip, detail="unknown account")
            raise AccountError("Wrong email or password.")
        now = time.time()
        if row["locked_until"] and row["locked_until"] > now:
            verify_password(password, _DUMMY_HASH)
            self.audit("login_locked", user_id=row["id"], email=email, ip=ip)
            mins = max(1, int((row["locked_until"] - now) / 60) + 1)
            raise AccountError(f"Too many failed attempts. Try again in {mins} minutes.")
        if not verify_password(password, row["pw_hash"]):
            fails = int(row["failed_logins"] or 0) + 1
            locked = 0.0
            if fails >= LOCK_AFTER:
                locked = now + min(LOCK_MAX_S, LOCK_BASE_MIN * 60 * 2 ** (fails - LOCK_AFTER))
                self.audit("lockout", user_id=row["id"], email=email, ip=ip, detail=f"{fails} failures")
            self._exec("UPDATE users SET failed_logins=?, locked_until=? WHERE id=?", (fails, locked, row["id"]))
            self.audit("login_failed", user_id=row["id"], email=email, ip=ip)
            raise AccountError("Wrong email or password.")
        if row["disabled"]:
            self.audit("login_disabled", user_id=row["id"], email=email, ip=ip)
            raise AccountError("This account is disabled.")
        self._exec("UPDATE users SET failed_logins=0, locked_until=0, last_login=? WHERE id=?", (now, row["id"]))
        self.audit("login", user_id=row["id"], email=email, ip=ip)
        return self._user(row)

    def get_user(self, user_id: int) -> User | None:
        row = self._one("SELECT * FROM users WHERE id=? AND disabled=0", (user_id,))
        return self._user(row) if row else None

    def list_users(self, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._all("SELECT id,email,created_at,is_admin,disabled,failed_logins,locked_until,"
                                           "last_login,signup_ip FROM users ORDER BY created_at DESC LIMIT ?", (limit,))]

    def count_users(self) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM users")["n"])

    def signups_from_ip(self, ip: str, since: float) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM users WHERE signup_ip=? AND created_at>?", (ip, since))["n"])

    def change_password(self, user_id: int, current: str, new: str, ip: str = "") -> None:
        row = self._one("SELECT * FROM users WHERE id=?", (user_id,))
        if not row or not verify_password(current, row["pw_hash"]):
            self.audit("password_change_failed", user_id=user_id, ip=ip)
            raise AccountError("Your current password is not correct.")
        if current == new:
            raise AccountError("Choose a password you have not used here before.")
        check_password_strength(new, row["email"])
        self._exec("UPDATE users SET pw_hash=?, password_changed=? WHERE id=?", (hash_password(new), time.time(), user_id))
        self.revoke_sessions(user_id)
        self.audit("password_changed", user_id=user_id, email=row["email"], ip=ip)

    def set_disabled(self, user_id: int, disabled: bool, by: str = "") -> None:
        self._exec("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, user_id))
        if disabled:
            self.revoke_sessions(user_id)
        self.audit("user_disabled" if disabled else "user_enabled", user_id=user_id, detail=f"by {by}")

    def delete_user(self, user_id: int, password: str, ip: str = "") -> list[str]:
        """Delete the account and its records; returns the run ids whose report files should be removed."""
        row = self._one("SELECT * FROM users WHERE id=?", (user_id,))
        if not row or not verify_password(password, row["pw_hash"]):
            raise AccountError("Your password is not correct.")
        runs = [r["run_id"] for r in self._all("SELECT run_id FROM jobs WHERE user_id=?", (user_id,))]
        for sql in ("DELETE FROM sessions WHERE user_id=?", "DELETE FROM usage WHERE user_id=?",
                    "DELETE FROM jobs WHERE user_id=?", "DELETE FROM users WHERE id=?"):
            self._exec(sql, (user_id,))
        self.audit("account_deleted", user_id=user_id, ip=ip, detail=f"{len(runs)} analyses removed")
        return runs

    # --------------------------------------------------------------- sessions
    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self._exec("INSERT INTO sessions(token_hash,user_id,created_at,expires_at,last_seen) VALUES(?,?,?,?,?)",
                   (_token_hash(token), user_id, now, now + SESSION_DAYS * 86400, now))
        return token

    def session_user(self, token: str | None) -> User | None:
        if not token or len(token) > 100:
            return None
        th = _token_hash(token)
        row = self._one("SELECT user_id, expires_at, last_seen FROM sessions WHERE token_hash=?", (th,))
        now = time.time()
        if not row or row["expires_at"] < now or (row["last_seen"] or 0) < now - SESSION_IDLE_DAYS * 86400:
            if row:
                self._exec("DELETE FROM sessions WHERE token_hash=?", (th,))
            return None
        if now - (row["last_seen"] or 0) > 300:  # refresh the idle clock at most every 5 minutes
            self._exec("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, th))
        return self.get_user(row["user_id"])

    def end_session(self, token: str | None) -> None:
        if token:
            self._exec("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))

    def revoke_sessions(self, user_id: int, keep_token: str | None = None) -> None:
        if keep_token:
            self._exec("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (user_id, _token_hash(keep_token)))
        else:
            self._exec("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def purge_expired_sessions(self) -> None:
        now = time.time()
        self._exec("DELETE FROM sessions WHERE expires_at<? OR last_seen<?", (now, now - SESSION_IDLE_DAYS * 86400))

    # ------------------------------------------------------------------- jobs
    def add_job(self, run_id: str, user_id: int, target: str, brief: dict, ip: str = "") -> None:
        self._exec("INSERT INTO jobs(run_id,user_id,target,brief,status,created_at,ip) VALUES(?,?,?,?,?,?,?)",
                   (run_id, user_id, target, json.dumps(brief), "queued", time.time(), ip))

    def set_job(self, run_id: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols} WHERE run_id=?", (*fields.values(), run_id))

    def job(self, run_id: str) -> dict | None:
        row = self._one("SELECT * FROM jobs WHERE run_id=?", (run_id,))
        return dict(row) if row else None

    def jobs_for(self, user_id: int, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                                           (user_id, limit))]

    def active_duplicate(self, user_id: int, target: str, within_s: float = 600) -> dict | None:
        """A queued or running job for the same company from the same user (double-click, impatient reload)."""
        row = self._one("SELECT * FROM jobs WHERE user_id=? AND lower(target)=lower(?) AND status IN ('queued','running') "
                        "AND created_at>? ORDER BY created_at DESC LIMIT 1", (user_id, target, time.time() - within_s))
        return dict(row) if row else None

    def all_jobs(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def queued_jobs(self) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at")]

    def queue_position(self, run_id: str) -> int:
        job = self.job(run_id)
        if not job or job["status"] != "queued":
            return 0
        return int(self._one("SELECT COUNT(*) AS n FROM jobs WHERE status='queued' AND created_at<=?",
                             (job["created_at"],))["n"])

    def recover_after_restart(self) -> list[dict]:
        """Jobs that were running when the server stopped cannot resume: mark them failed. Queued jobs stay queued."""
        self._exec("UPDATE jobs SET status='failed', error='Interrupted by a server restart. Please start it again.', "
                   "finished_at=? WHERE status='running'", (time.time(),))
        return self.queued_jobs()

    def jobs_since(self, since: float, user_id: int | None = None, ip: str | None = None) -> int:
        sql, params = "SELECT COUNT(*) AS n FROM jobs WHERE created_at>?", [since]
        if user_id is not None:
            sql += " AND user_id=?"
            params.append(user_id)
        if ip is not None:
            sql += " AND ip=?"
            params.append(ip)
        return int(self._one(sql, tuple(params))["n"])

    # ------------------------------------------------------------------ usage
    def record_usage(self, user_id: int, kind: str) -> None:
        self._exec("INSERT INTO usage(user_id,kind,ts) VALUES(?,?,?)", (user_id, kind, time.time()))

    def usage_since(self, user_id: int, kind: str, since: float) -> int:
        return int(self._one("SELECT COUNT(*) AS n FROM usage WHERE user_id=? AND kind=? AND ts>?",
                             (user_id, kind, since))["n"])


class RateLimiter:
    """In-memory sliding-window limiter for log-in and sign-up attempts (per IP and per email)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def hit(self, key: str, limit: int, window_s: float) -> bool:
        """Record an attempt; False when the key is over its limit."""
        now = time.time()
        with self._lock:
            q = self._hits[key]
            while q and q[0] < now - window_s:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 50_000:  # bound memory under a flood of distinct keys
                for k in [k for k, v in self._hits.items() if not v or v[-1] < now - window_s][:10_000]:
                    self._hits.pop(k, None)
            return True


def start_of_day(now: float | None = None) -> float:
    """Midnight UTC today (daily limits reset at 00:00 UTC)."""
    now = time.time() if now is None else now
    return now - (now % 86400)


__all__ = ["AccountError", "Accounts", "RateLimiter", "User", "blocked_email_domain", "canonical_email",
           "check_password_strength", "hash_password", "normalise_email", "start_of_day", "verify_password"]
