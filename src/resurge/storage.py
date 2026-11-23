"""SQLite persistence for the whole company.

One file (``data/resurge.db``) holds:
  * kv_cache      - LLM / search / page caches (implements ``KVCache``)
  * runs, events  - run registry + the company activity feed
  * llm_calls     - per-call telemetry (tokens, latency, success)
  * quota_usage   - persistent daily request/token counters per model & provider
  * model_health  - auto-disabled models + learned capability flags
  * model_stats   - per (role, model) success/latency/quality -> router learning
  * source_stats  - per-domain verification outcomes -> learned source reputation
  * facts         - knowledge base of verified facts (incremental runs, MCP queries, diffs)
  * playbook      - ACE-style lessons per role with helpful/harmful counters
  * run_scores    - quality scorecard history (is the company improving?)

SQLite operations here are sub-millisecond, so they run synchronously behind a
lock; WAL mode lets the web dashboard read while a run is writing.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_cache(
  namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  expires_at REAL, created_at REAL NOT NULL, PRIMARY KEY(namespace, key));
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, created_at REAL NOT NULL, target TEXT, status TEXT,
  brief TEXT, report_dir TEXT, metrics TEXT, quality REAL, finished_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, department TEXT,
  agent TEXT, kind TEXT, message TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE TABLE IF NOT EXISTS llm_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, ts REAL, agent TEXT, role TEXT,
  model_id TEXT, family TEXT, tokens_in INT, tokens_out INT, latency_ms INT,
  ok INT, cached INT, error TEXT);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);
CREATE TABLE IF NOT EXISTS quota_usage(
  key TEXT NOT NULL, day TEXT NOT NULL, requests INT NOT NULL DEFAULT 0,
  tokens INT NOT NULL DEFAULT 0, PRIMARY KEY(key, day));
CREATE TABLE IF NOT EXISTS model_health(
  model_id TEXT PRIMARY KEY, disabled_until REAL, reason TEXT,
  json_mode INT, system_prompt INT, updated_at REAL);
CREATE TABLE IF NOT EXISTS model_stats(
  role TEXT NOT NULL, model_id TEXT NOT NULL, successes REAL NOT NULL DEFAULT 0,
  failures REAL NOT NULL DEFAULT 0, calls INT NOT NULL DEFAULT 0,
  latency_ms_total INT NOT NULL DEFAULT 0, quality_sum REAL NOT NULL DEFAULT 0,
  quality_n INT NOT NULL DEFAULT 0, updated_at REAL, PRIMARY KEY(role, model_id));
CREATE TABLE IF NOT EXISTS source_stats(
  domain TEXT PRIMARY KEY, supported INT NOT NULL DEFAULT 0, partial INT NOT NULL DEFAULT 0,
  unsupported INT NOT NULL DEFAULT 0, total INT NOT NULL DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS facts(
  id TEXT NOT NULL, run_id TEXT NOT NULL, target TEXT, competitor TEXT, dimension TEXT,
  claim TEXT, value TEXT, status TEXT, confidence REAL, data TEXT, created_at REAL,
  PRIMARY KEY(id, run_id));
CREATE INDEX IF NOT EXISTS idx_facts_comp ON facts(competitor, dimension);
CREATE TABLE IF NOT EXISTS playbook(
  id TEXT PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL, helpful INT NOT NULL DEFAULT 0,
  harmful INT NOT NULL DEFAULT 0, uses INT NOT NULL DEFAULT 0, source_run TEXT,
  active INT NOT NULL DEFAULT 1, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_playbook_role ON playbook(role, active);
CREATE TABLE IF NOT EXISTS run_scores(
  run_id TEXT NOT NULL, metric TEXT NOT NULL, value REAL, created_at REAL,
  PRIMARY KEY(run_id, metric));
"""


def _dumps(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class Storage:
    """Thread-safe SQLite store. Also a ``KVCache`` implementation."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ core
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------- KV cache
    async def get(self, namespace: str, key: str) -> Any | None:
        return self.kv_get(namespace, key)

    async def set(self, namespace: str, key: str, value: Any, ttl_s: int | None = None) -> None:
        self.kv_set(namespace, key, value, ttl_s)

    def kv_get(self, namespace: str, key: str) -> Any | None:
        rows = self._query("SELECT value, expires_at FROM kv_cache WHERE namespace=? AND key=?", (namespace, key))
        if not rows:
            return None
        exp = rows[0]["expires_at"]
        if exp is not None and exp < time.time():
            self._exec("DELETE FROM kv_cache WHERE namespace=? AND key=?", (namespace, key))
            return None
        return json.loads(rows[0]["value"])

    def kv_set(self, namespace: str, key: str, value: Any, ttl_s: int | None = None) -> None:
        now = time.time()
        self._exec(
            "INSERT OR REPLACE INTO kv_cache(namespace,key,value,expires_at,created_at) VALUES(?,?,?,?,?)",
            (namespace, key, _dumps(value), now + ttl_s if ttl_s else None, now),
        )

    def kv_delete(self, namespace: str, key: str) -> None:
        self._exec("DELETE FROM kv_cache WHERE namespace=? AND key=?", (namespace, key))

    def purge_expired(self) -> int:
        cur = self._exec("DELETE FROM kv_cache WHERE expires_at IS NOT NULL AND expires_at < ?", (time.time(),))
        return cur.rowcount

    def cache_stats(self) -> dict[str, int]:
        rows = self._query("SELECT namespace, COUNT(*) n FROM kv_cache GROUP BY namespace")
        return {r["namespace"]: r["n"] for r in rows}

    def clear_cache(self, namespace: str | None = None) -> int:
        if namespace:
            return self._exec("DELETE FROM kv_cache WHERE namespace=?", (namespace,)).rowcount
        return self._exec("DELETE FROM kv_cache").rowcount

    # ----------------------------------------------------------------- runs
    def create_run(self, run_id: str, target: str, brief: dict) -> None:
        self._exec(
            "INSERT OR REPLACE INTO runs(id,created_at,target,status,brief) VALUES(?,?,?,?,?)",
            (run_id, time.time(), target, "running", _dumps(brief)),
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k}=?")
            vals.append(_dumps(v) if isinstance(v, (dict, list)) else v)
        self._exec(f"UPDATE runs SET {', '.join(cols)} WHERE id=?", (*vals, run_id))

    def delete_run(self, run_id: str) -> None:
        """Remove a run and everything recorded about it (its events, model calls, facts and scores)."""
        for sql in ("DELETE FROM events WHERE run_id=?", "DELETE FROM llm_calls WHERE run_id=?",
                    "DELETE FROM facts WHERE run_id=?", "DELETE FROM run_scores WHERE run_id=?",
                    "DELETE FROM runs WHERE id=?"):
            self._exec(sql, (run_id,))

    def get_run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM runs WHERE id=?", (run_id,))
        return self._run_row(rows[0]) if rows else None

    def list_runs(self, limit: int = 50) -> list[dict]:
        return [self._run_row(r) for r in self._query("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def latest_run_for(self, target: str, status: str = "done", exclude: str | None = None) -> dict | None:
        rows = self._query(
            "SELECT * FROM runs WHERE lower(target)=lower(?) AND status=? AND id != ? ORDER BY created_at DESC LIMIT 1",
            (target, status, exclude or ""),
        )
        return self._run_row(rows[0]) if rows else None

    STALE_AFTER_S = 24 * 3600

    @staticmethod
    def _run_row(r: sqlite3.Row) -> dict:
        d = dict(r)
        if d.get("status") == "running" and not d.get("finished_at") and \
                time.time() - (d.get("created_at") or 0) > Storage.STALE_AFTER_S:
            d["status"] = "stale"  # process died without cleanup (crash, power loss)
        for k in ("brief", "metrics"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except (TypeError, json.JSONDecodeError):
                    pass
        return d

    # --------------------------------------------------------------- events
    def add_event(self, run_id: str | None, department: str, agent: str, kind: str, message: str, data: dict | None = None) -> int:
        cur = self._exec(
            "INSERT INTO events(run_id,ts,department,agent,kind,message,data) VALUES(?,?,?,?,?,?,?)",
            (run_id, time.time(), department, agent, kind, message, _dumps(data or {})),
        )
        return int(cur.lastrowid or 0)

    def get_events(self, run_id: str, after_id: int = 0, limit: int = 500) -> list[dict]:
        rows = self._query(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?", (run_id, after_id, limit)
        )
        out = []
        for r in rows:
            d = dict(r)
            d["data"] = json.loads(d["data"]) if d.get("data") else {}
            out.append(d)
        return out

    # ------------------------------------------------------------ telemetry
    def record_llm_call(self, *, run_id: str | None, agent: str, role: str, model_id: str, family: str,
                        tokens_in: int, tokens_out: int, latency_ms: int, ok: bool, cached: bool,
                        error: str | None = None) -> None:
        self._exec(
            "INSERT INTO llm_calls(run_id,ts,agent,role,model_id,family,tokens_in,tokens_out,latency_ms,ok,cached,error)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, time.time(), agent, role, model_id, family, tokens_in, tokens_out, latency_ms,
             int(ok), int(cached), (error or "")[:500]),
        )

    def llm_calls_for_run(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM llm_calls WHERE run_id=? ORDER BY id", (run_id,))]

    # ---------------------------------------------------------------- quota
    def get_usage(self, key: str, day: str) -> tuple[int, int]:
        rows = self._query("SELECT requests, tokens FROM quota_usage WHERE key=? AND day=?", (key, day))
        return (rows[0]["requests"], rows[0]["tokens"]) if rows else (0, 0)

    def add_usage(self, key: str, day: str, requests: int, tokens: int) -> None:
        self._exec(
            "INSERT INTO quota_usage(key,day,requests,tokens) VALUES(?,?,?,?) "
            "ON CONFLICT(key,day) DO UPDATE SET requests=requests+excluded.requests, tokens=tokens+excluded.tokens",
            (key, day, requests, tokens),
        )

    def set_usage_exhausted(self, key: str, day: str, limit: int) -> None:
        self._exec(
            "INSERT INTO quota_usage(key,day,requests,tokens) VALUES(?,?,?,0) "
            "ON CONFLICT(key,day) DO UPDATE SET requests=MAX(requests, excluded.requests)",
            (key, day, limit),
        )

    def usage_for_day(self, day: str) -> dict[str, tuple[int, int]]:
        return {r["key"]: (r["requests"], r["tokens"]) for r in self._query("SELECT * FROM quota_usage WHERE day=?", (day,))}

    # --------------------------------------------------------- model health
    def get_health(self, model_id: str) -> dict:
        rows = self._query("SELECT * FROM model_health WHERE model_id=?", (model_id,))
        return dict(rows[0]) if rows else {}

    def all_health(self) -> dict[str, dict]:
        return {r["model_id"]: dict(r) for r in self._query("SELECT * FROM model_health")}

    def set_disabled(self, model_id: str, until: float, reason: str) -> None:
        self._exec(
            "INSERT INTO model_health(model_id,disabled_until,reason,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(model_id) DO UPDATE SET disabled_until=excluded.disabled_until, reason=excluded.reason, updated_at=excluded.updated_at",
            (model_id, until, reason[:300], time.time()),
        )

    def set_capability(self, model_id: str, *, json_mode: bool | None = None, system_prompt: bool | None = None) -> None:
        h = self.get_health(model_id)
        jm = int(json_mode) if json_mode is not None else h.get("json_mode")
        sp = int(system_prompt) if system_prompt is not None else h.get("system_prompt")
        self._exec(
            "INSERT INTO model_health(model_id,json_mode,system_prompt,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(model_id) DO UPDATE SET json_mode=excluded.json_mode, system_prompt=excluded.system_prompt, updated_at=excluded.updated_at",
            (model_id, jm, sp, time.time()),
        )

    def reset_health(self) -> None:
        self._exec("DELETE FROM model_health")

    # ---------------------------------------------------------- model stats
    def update_model_stat(self, role: str, model_id: str, *, success: float, latency_ms: int = 0,
                          quality: float | None = None) -> None:
        """success in [0,1] (fractional allowed: e.g. 0.5 for 'valid JSON but needed repair')."""
        self._exec(
            "INSERT INTO model_stats(role,model_id,successes,failures,calls,latency_ms_total,quality_sum,quality_n,updated_at)"
            " VALUES(?,?,?,?,1,?,?,?,?) ON CONFLICT(role,model_id) DO UPDATE SET"
            " successes=successes+excluded.successes, failures=failures+excluded.failures, calls=calls+1,"
            " latency_ms_total=latency_ms_total+excluded.latency_ms_total,"
            " quality_sum=quality_sum+excluded.quality_sum, quality_n=quality_n+excluded.quality_n,"
            " updated_at=excluded.updated_at",
            (role, model_id, success, 1.0 - success, latency_ms, quality or 0.0, 1 if quality is not None else 0, time.time()),
        )

    def add_model_quality(self, role: str, model_id: str, quality: float) -> None:
        """Attach a downstream quality signal (e.g. validator pass-rate) without counting a call."""
        self._exec(
            "INSERT INTO model_stats(role,model_id,quality_sum,quality_n,updated_at) VALUES(?,?,?,1,?) "
            "ON CONFLICT(role,model_id) DO UPDATE SET quality_sum=quality_sum+excluded.quality_sum,"
            " quality_n=quality_n+1, updated_at=excluded.updated_at",
            (role, model_id, quality, time.time()),
        )

    def get_model_stats(self, role: str | None = None) -> list[dict]:
        if role:
            return [dict(r) for r in self._query("SELECT * FROM model_stats WHERE role=?", (role,))]
        return [dict(r) for r in self._query("SELECT * FROM model_stats ORDER BY role, calls DESC")]

    # --------------------------------------------------------- source stats
    def update_source(self, domain: str, status: str) -> None:
        col = {"supported": "supported", "partial": "partial"}.get(status, "unsupported")
        self._exec(
            f"INSERT INTO source_stats(domain,{col},total,updated_at) VALUES(?,1,1,?) "
            f"ON CONFLICT(domain) DO UPDATE SET {col}={col}+1, total=total+1, updated_at=excluded.updated_at",
            (domain, time.time()),
        )

    def source_reliability(self, domain: str, prior: float, strength: float = 4.0) -> float:
        """Bayesian-smoothed reliability: verified outcomes pull the prior up or down."""
        rows = self._query("SELECT * FROM source_stats WHERE domain=?", (domain,))
        if not rows:
            return prior
        r = rows[0]
        good = r["supported"] + 0.5 * r["partial"]
        return (good + prior * strength) / (r["total"] + strength)

    def top_sources(self, limit: int = 30) -> list[dict]:
        return [dict(r) for r in self._query("SELECT * FROM source_stats ORDER BY total DESC LIMIT ?", (limit,))]

    # ---------------------------------------------------------------- facts
    def save_facts(self, run_id: str, target: str, facts: list[dict]) -> None:
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO facts(id,run_id,target,competitor,dimension,claim,value,status,confidence,data,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (f["id"], run_id, target, f.get("competitor"), f.get("dimension"), f.get("claim"), f.get("value"),
                     f.get("status"), f.get("confidence", 0.0), _dumps(f), now)
                    for f in facts
                ],
            )
            self._conn.commit()

    def get_facts(self, *, competitor: str | None = None, run_id: str | None = None, max_age_s: float | None = None,
                  usable_only: bool = True, limit: int = 2000) -> list[dict]:
        sql, params = "SELECT data, created_at, run_id FROM facts WHERE 1=1", []
        if competitor:
            sql += " AND lower(competitor)=lower(?)"
            params.append(competitor)
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        if max_age_s:
            sql += " AND created_at>=?"
            params.append(time.time() - max_age_s)
        if usable_only:
            sql += " AND status IN ('supported','partial')"
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        out = []
        for r in self._query(sql, params):
            d = json.loads(r["data"])
            d["_run_id"], d["_created_at"] = r["run_id"], r["created_at"]
            out.append(d)
        return out

    def search_facts(self, text: str, limit: int = 30) -> list[dict]:
        like = f"%{text.lower()}%"
        rows = self._query(
            "SELECT data FROM facts WHERE status IN ('supported','partial') AND (lower(claim) LIKE ? OR lower(competitor) LIKE ?"
            " OR lower(dimension) LIKE ? OR lower(value) LIKE ?) ORDER BY confidence DESC, created_at DESC LIMIT ?",
            (like, like, like, like, limit),
        )
        return [json.loads(r["data"]) for r in rows]

    # ------------------------------------------------------------- playbook
    def get_lessons(self, role: str, limit: int = 12) -> list[dict]:
        rows = self._query(
            "SELECT * FROM playbook WHERE role=? AND active=1 ORDER BY (helpful - harmful) DESC, updated_at DESC LIMIT ?",
            (role, limit),
        )
        return [dict(r) for r in rows]

    def all_lessons(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM playbook" + (" WHERE active=1" if active_only else "") + " ORDER BY role, (helpful-harmful) DESC"
        return [dict(r) for r in self._query(sql)]

    def upsert_lesson(self, lesson_id: str, role: str, text: str, source_run: str | None) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO playbook(id,role,text,source_run,created_at,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, active=1, updated_at=excluded.updated_at",
            (lesson_id, role, text, source_run, now, now),
        )

    def lesson_feedback(self, lesson_ids: list[str], *, helpful: int = 0, harmful: int = 0, used: bool = False) -> None:
        if not lesson_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE playbook SET helpful=helpful+?, harmful=harmful+?, uses=uses+?, updated_at=? WHERE id=?",
                [(helpful, harmful, int(used), time.time(), lid) for lid in lesson_ids],
            )
            self._conn.commit()

    def deactivate_lesson(self, lesson_id: str) -> None:
        self._exec("UPDATE playbook SET active=0, updated_at=? WHERE id=?", (time.time(), lesson_id))

    # ------------------------------------------------------- data retention
    def clear_knowledge(self) -> int:
        return self._exec("DELETE FROM facts").rowcount

    def reset_learning(self) -> None:
        with self._lock:
            for table in ("playbook", "model_stats", "source_stats", "run_scores", "model_health"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.execute("DELETE FROM kv_cache WHERE namespace='learn'")
            self._conn.commit()

    def clear_history(self) -> None:
        with self._lock:
            for table in ("runs", "events", "llm_calls", "quota_usage"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    # ----------------------------------------------------------- scorecards
    def set_scores(self, run_id: str, scores: dict[str, float]) -> None:
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO run_scores(run_id,metric,value,created_at) VALUES(?,?,?,?)",
                [(run_id, k, float(v), now) for k, v in scores.items()],
            )
            self._conn.commit()

    def score_history(self, metric: str = "quality_score", limit: int = 20) -> list[tuple[str, float]]:
        rows = self._query(
            "SELECT run_id, value FROM run_scores WHERE metric=? ORDER BY created_at DESC LIMIT ?", (metric, limit)
        )
        return [(r["run_id"], r["value"]) for r in reversed(rows)]
