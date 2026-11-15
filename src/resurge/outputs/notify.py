"""Optional run notifications through webhooks (Slack, Discord, generic JSON).

Configured with ``SLACK_WEBHOOK_URL``, ``DISCORD_WEBHOOK_URL`` and/or
``RESURGE_WEBHOOK_URL``. Each message carries the headline, three key findings,
the top threats, the quality score and where the deliverables are. Nothing is
sent when no webhook is configured, and delivery failures are logged, never raised.

All digest text is untrusted, so mention syntax is broken before anything is
sent (``@everyone``/``@here``/``@channel``, Slack ``<!channel>``/``<@U…>``,
Discord ``<@id>``/``<@&role>``/``<#channel>``); Slack messages disable link
unfurling and Discord messages allow no mentions and escape Markdown, so a
planted ``[click](…)`` masked link renders as plain text.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from resurge.outputs.tables import clamp_int, clip, fmt_score, one_line, pct, report_title, target_name
from resurge.schemas import ResearchReport

log = logging.getLogger(__name__)

ENV_SLACK = "SLACK_WEBHOOK_URL"
ENV_DISCORD = "DISCORD_WEBHOOK_URL"
ENV_GENERIC = "RESURGE_WEBHOOK_URL"
TIMEOUT_S = 10.0
BRAND_COLOR = 0x14B8A6
ZWSP = "\u200b"  # zero-width space: keeps text readable, breaks mention parsing

_MASS_MENTION = re.compile(r"(?<!\w)[@＠](?=(?:everyone|here|channel|all)\b)", re.IGNORECASE)
_TAG_MENTION = re.compile(r"<(?=\s*[@!#])")  # <@U123> <@!123> <@&role> <!channel> <!here> <#C123>
_DISCORD_MARKDOWN = re.compile(r"([\\*_~`|>\[\]()#])")


def neutralize_mentions(value: object) -> str:
    """Single-line text whose chat mentions (mass, user, role, channel) can no longer fire."""
    text = _TAG_MENTION.sub("<" + ZWSP, one_line(value))
    return _MASS_MENTION.sub(lambda match: match.group(0) + ZWSP, text)


@dataclass(frozen=True)
class Digest:
    """The notification content, independent of channel formatting."""

    run_id: str
    title: str
    target: str
    created_at: str
    headline: str
    findings: list[str] = field(default_factory=list)
    threats: list[str] = field(default_factory=list)
    quality_score: float = 0.0
    verification_rate: float = 0.0
    artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def quality_line(self) -> str:
        return f"Quality {fmt_score(self.quality_score, 100)} · verification rate {pct(self.verification_rate)}"


def make_digest(report: ResearchReport, artifacts: Mapping[str, Any]) -> Digest:
    """Collect headline, findings, threats, quality and artifact locations."""
    es = report.executive_summary
    threats = [one_line(t) for t in es.top_threats if one_line(t)][:3]
    if not threats:
        ranked = sorted((p for p in report.profiles if p.threat_level is not None),
                        key=lambda p: -clamp_int(p.threat_level, 1, 5))
        threats = [f"{p.name} (threat {clamp_int(p.threat_level, 1, 5)}/5)" for p in ranked[:3]]
    m = report.metrics
    verification = m.verification_rate or (m.facts_usable / m.facts_total if m.facts_total else 0.0)
    safe = neutralize_mentions
    return Digest(
        run_id=safe(report.run_id),
        title=safe(report_title(report)),
        target=safe(target_name(report)),
        created_at=report.created_at.isoformat(),
        headline=safe(one_line(es.headline) or one_line(es.summary) or "Research run completed."),
        findings=[safe(f.text) for f in es.key_findings[:3] if one_line(f.text)],
        threats=[safe(t) for t in threats],
        quality_score=m.quality_score,
        verification_rate=verification,
        artifacts={safe(k): safe(v) for k, v in artifacts.items() if v and not str(k).endswith("_error")},
    )


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_text(text: str, limit: int) -> str:
    """Escaped Slack text that still fits ``limit`` characters after escaping."""
    budget = limit
    while True:
        out = _slack_escape(clip(text, budget))
        if len(out) <= limit or budget <= 1:
            return out[:limit]
        budget -= max(1, len(out) - limit)


def slack_payload(d: Digest) -> dict[str, Any]:
    """Slack incoming-webhook message (Block Kit with a plain-text fallback)."""

    def section(title: str, lines: list[str]) -> dict[str, Any]:
        body = "\n".join(f"• {_slack_escape(clip(line, 280))}" for line in lines)
        return {"type": "section", "text": {"type": "mrkdwn", "text": clip(f"*{title}*\n{body}", 2900)}}

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _slack_text(d.title, 150)}},
        {"type": "section", "text": {"type": "mrkdwn", "text": _slack_text(d.headline, 2900)}},
    ]
    if d.findings:
        blocks.append(section("Key findings", d.findings))
    if d.threats:
        blocks.append(section("Top threats", d.threats))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": _slack_escape(
        f"{d.quality_line} · run {d.run_id}")}]})
    if d.artifacts:
        blocks.append(section("Deliverables", [f"{k}: {v}" for k, v in d.artifacts.items()]))
    return {
        "text": _slack_text(f"{d.title}: {d.headline}", 3000),
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def _discord_escape(text: str) -> str:
    return _DISCORD_MARKDOWN.sub(r"\\\1", text)


def discord_payload(d: Digest) -> dict[str, Any]:
    """Discord webhook message with one embed (field limits respected, no mentions)."""
    fields = []
    for name, lines in (("Key findings", d.findings), ("Top threats", d.threats),
                        ("Deliverables", [f"{k}: {v}" for k, v in d.artifacts.items()])):
        if lines:
            body = "\n".join(f"• {_discord_escape(clip(line, 300))}" for line in lines)
            fields.append({"name": name, "value": clip(body, 1000)})
    fields.append({"name": "Quality", "value": clip(d.quality_line, 1000)})
    return {
        "username": "Resurge",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": _discord_escape(clip(d.title, 200)),
            "description": _discord_escape(clip(d.headline, 3000)),
            "color": BRAND_COLOR,
            "fields": fields,
            "footer": {"text": clip(f"Run {d.run_id} · Generated by Resurge", 2000)},
        }],
    }


def generic_payload(d: Digest) -> dict[str, Any]:
    """Plain JSON event for any webhook consumer."""
    return {
        "event": "resurge.report.published",
        "run_id": d.run_id,
        "title": d.title,
        "target": d.target,
        "created_at": d.created_at,
        "headline": d.headline,
        "key_findings": d.findings,
        "top_threats": d.threats,
        "quality_score": d.quality_score,
        "verification_rate": d.verification_rate,
        "artifacts": d.artifacts,
    }


CHANNELS: tuple[tuple[str, str, Callable[[Digest], dict[str, Any]]], ...] = (
    ("slack", ENV_SLACK, slack_payload),
    ("discord", ENV_DISCORD, discord_payload),
    ("webhook", ENV_GENERIC, generic_payload),
)


def notify(
    report: ResearchReport,
    artifacts: Mapping[str, Path | str],
    *,
    env: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> list[str]:
    """Post the run digest to every configured webhook; returns the channels that accepted it."""
    env = os.environ if env is None else env
    targets = [(name, url, build) for name, var, build in CHANNELS if (url := (env.get(var) or "").strip())]
    if not targets:
        return []
    digest = make_digest(report, artifacts)
    http = client or httpx.Client(timeout=TIMEOUT_S, follow_redirects=False)
    sent: list[str] = []
    try:
        for name, url, build in targets:
            try:
                response = http.post(url, json=build(digest))
                response.raise_for_status()
                sent.append(name)
            except Exception as exc:  # network/HTTP errors must never break a run
                log.warning("Notification via %s failed: %s", name, exc)
    finally:
        if client is None:
            http.close()
    return sent
