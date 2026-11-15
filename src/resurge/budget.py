"""The CFO: per-run budgets that keep a run inside free-tier quotas.

Departments ask ``budget.can(kind)`` before spending; when pressure rises the
orchestrator trims optional work (follow-up research rounds, revision loops,
battlecards) instead of failing. ``depth`` presets trade thoroughness for quota.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .schemas import Depth


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class BudgetLimits:
    llm_calls: int
    tokens: int
    searches: int
    pages: int
    competitors: int
    dimensions: int  # max research dimensions per competitor
    follow_up_rounds: int
    revision_rounds: int
    pages_per_competitor: int
    results_per_query: int


PRESETS: dict[Depth, BudgetLimits] = {
    Depth.quick: BudgetLimits(llm_calls=45, tokens=450_000, searches=40, pages=24, competitors=3, dimensions=6,
                              follow_up_rounds=0, revision_rounds=0, pages_per_competitor=6, results_per_query=5),
    Depth.standard: BudgetLimits(llm_calls=110, tokens=1_400_000, searches=120, pages=70, competitors=4, dimensions=9,
                                 follow_up_rounds=1, revision_rounds=1, pages_per_competitor=12, results_per_query=6),
    Depth.deep: BudgetLimits(llm_calls=260, tokens=3_500_000, searches=300, pages=170, competitors=6, dimensions=11,
                             follow_up_rounds=2, revision_rounds=2, pages_per_competitor=22, results_per_query=8),
}


class Budget:
    def __init__(self, depth: Depth | str = Depth.standard, **overrides: int) -> None:
        depth = Depth(depth)
        base = asdict(PRESETS[depth])
        base.update({k: v for k, v in overrides.items() if v is not None and k in base})
        self.depth = depth
        self.limits = BudgetLimits(**base)
        self.used: dict[str, int] = {"llm_calls": 0, "tokens": 0, "searches": 0, "pages": 0}
        self.reserved_llm_calls = 0  # held back during research so analysis/reporting can always finish

    def can(self, kind: str, n: int = 1) -> bool:
        limit = getattr(self.limits, kind)
        if kind == "llm_calls":
            limit -= self.reserved_llm_calls
        return self.used.get(kind, 0) + n <= limit

    def spend(self, kind: str, n: int = 1) -> None:
        self.used[kind] = self.used.get(kind, 0) + n

    def remaining(self, kind: str) -> int:
        return max(0, getattr(self.limits, kind) - self.used.get(kind, 0))

    def pressure(self) -> float:
        """Highest fraction of any consumable budget already used (0..1+)."""
        return max(self.used[k] / max(1, getattr(self.limits, k)) for k in self.used)

    def llm_pressure(self) -> float:
        """Fraction of the LLM budget (calls or tokens) used - the one that matters for LLM-only work."""
        return max(self.used["llm_calls"] / max(1, self.limits.llm_calls),
                   self.used["tokens"] / max(1, self.limits.tokens))

    def snapshot(self) -> dict:
        return {"depth": self.depth.value, "used": dict(self.used), "limits": asdict(self.limits),
                "pressure": round(self.pressure(), 3)}
