"""Machine-readable export: the full report as JSON.

``report.json`` round-trips through ``ResearchReport.model_validate_json``; it is
what ``resurge publish`` re-renders from and what the chat index is built on.
"""

from __future__ import annotations

from pathlib import Path

from resurge.schemas import ResearchReport


def export_json(report: ResearchReport, out_dir: Path) -> dict[str, Path]:
    """Write ``report.json`` into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "report.json"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return {"json": json_path}
