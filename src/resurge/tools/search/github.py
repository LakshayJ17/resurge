"""GitHub repository search (free; ``GITHUB_TOKEN`` optional for higher limits) - kind ``code``.

Unauthenticated search is limited to ~10 requests/minute; when GitHub reports
the limit (403/429 with ``x-ratelimit-remaining: 0``) the backend cools down
until the advertised reset so the router falls through immediately.
"""

from __future__ import annotations

from typing import Any

from ...schemas import SearchResult
from ..http import HttpResult, retry_after_s
from .base import SearchBackend, SearchError, parse_date, result, since, strip_site_operators


class GitHubBackend(SearchBackend):
    """GitHub ``/search/repositories`` (best-match order)."""

    name = "github"
    kinds = {"code"}
    requires_key = False
    env_vars = ["GITHUB_TOKEN"]  # optional
    tier = "specialty"
    timeout_s = 15.0
    API_URL = "https://api.github.com/search/repositories"

    def pacing_s(self) -> float:
        """2 s between calls keyless (search allows ~10/min), 0.5 s with a token."""
        if self._pace_override is not None:
            return max(0.0, self._pace_override)
        return 0.5 if (self._env("GITHUB_TOKEN") or self._env("GH_TOKEN")) else 2.0

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token := self._env("GITHUB_TOKEN") or self._env("GH_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def search(
        self,
        query: str,
        *,
        kind: str = "code",
        max_results: int = 8,
        timelimit: str | None = None,
        region: str | None = None,
    ) -> list[SearchResult]:
        self._check(kind)
        await self._pace()
        q = strip_site_operators(query)
        if start := since(timelimit):
            q = f"{q} pushed:>={start.date().isoformat()}"
        res = await self._request("GET", self.API_URL, params={"q": q, "per_page": max(1, min(max_results, 50))},
                                  headers=self._headers())
        return self._finalize(self.parse_repos(self._json(res)), kind, max_results)

    def _raise_for_status(self, res: HttpResult) -> None:
        if res.status in (403, 429) and (
            res.status == 429 or res.headers.get("x-ratelimit-remaining") == "0" or b"rate limit" in res.content.lower()
        ):
            wait = retry_after_s(res.headers, default=60.0)
            self.cooldown(wait, "rate limited")
            raise SearchError(f"rate limited; cooling down {wait:.0f}s", backend=self.name, status=res.status)
        super()._raise_for_status(res)

    @staticmethod
    def parse_repos(data: Any) -> list[SearchResult]:
        """Results from a ``/search/repositories`` response."""
        out = []
        for repo in (data or {}).get("items") or []:
            meta = [f"★ {int(repo.get('stargazers_count') or 0):,}"]
            if repo.get("language"):
                meta.append(str(repo["language"]))
            meta.append(f"forks {int(repo.get('forks_count') or 0):,}")
            pushed = repo.get("pushed_at") or repo.get("updated_at")
            if date := parse_date(pushed):
                meta.append(f"updated {date[:10]}")
            if spdx := ((repo.get("license") or {}).get("spdx_id")):
                if spdx != "NOASSERTION":
                    meta.append(spdx)
            if repo.get("archived"):
                meta.append("archived")
            snippet = f"{repo.get('description') or 'No description'} ({' · '.join(meta)})."
            if topics := repo.get("topics"):
                snippet += f" Topics: {', '.join(topics[:8])}."
            if homepage := repo.get("homepage"):
                snippet += f" Homepage: {homepage}"
            out.append(result(repo.get("html_url"), repo.get("full_name") or repo.get("name"), snippet, pushed))
        return out
