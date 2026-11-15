"""Company activity feed: every hand-off, decision, review and LLM call is an event.

Events fan out to (a) SQLite (``events`` table, replayable in the dashboard),
(b) synchronous callbacks (the Rich console), and (c) asyncio queues (SSE).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

# Departments of the Resurge company
EXEC = "Executive Office"
STRATEGY = "Strategy & Planning"
RESEARCH = "Research"
QA = "Quality Assurance"
ANALYSIS = "Analysis"
RISK = "Risk Office"
COMMS = "Design & Communications"
LEARNING = "Learning & Development"
INFRA = "Infrastructure (CTO)"
FINANCE = "Finance (CFO)"

DEPARTMENTS = [EXEC, STRATEGY, RESEARCH, QA, ANALYSIS, RISK, COMMS, LEARNING, INFRA, FINANCE]


@dataclass
class Event:
    run_id: str | None
    department: str
    agent: str
    kind: str  # start | done | info | warn | error | handoff | review | llm | metric
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventBus:
    def __init__(self, storage=None, run_id: str | None = None) -> None:
        self.storage = storage
        self.run_id = run_id
        self._callbacks: list[Callable[[Event], None]] = []
        self._queues: set[asyncio.Queue] = set()
        self.history: list[Event] = []

    def subscribe(self, cb: Callable[[Event], None]) -> None:
        self._callbacks.append(cb)

    def unsubscribe(self, cb: Callable[[Event], None]) -> None:
        with contextlib.suppress(ValueError):
            self._callbacks.remove(cb)

    def queue(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.add(q)
        return q

    def drop_queue(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def emit(self, department: str, agent: str, kind: str, message: str, **data: Any) -> Event:
        ev = Event(self.run_id, department, agent, kind, message, data)
        if self.storage is not None:
            try:
                ev.id = self.storage.add_event(self.run_id, department, agent, kind, message, data)
            except Exception:  # noqa: BLE001 - logging must never break the pipeline
                pass
        self.history.append(ev)
        if len(self.history) > 5000:
            self.history = self.history[-4000:]
        for cb in list(self._callbacks):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001
                pass
        for q in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(ev)
        return ev
