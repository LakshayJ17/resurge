"""Machine-readable exports: the full report as JSON plus facts/sources CSVs.

``report.json`` round-trips through ``ResearchReport.model_validate_json``; the
CSVs are written from the same shared tables as the workbook and Google Sheet,
with OWASP CSV-injection protection: text cells a spreadsheet would evaluate
(leading ``= + - @``, tab, CR or their full-width forms) get a ``'`` prefix.
"""

from __future__ import annotations

import csv
from pathlib import Path

from resurge.outputs.tables import Table, escape_formula, facts_table, sources_table
from resurge.schemas import ResearchReport


def write_csv(table: Table, path: Path) -> Path:
    """Write a shared table as RFC 4180 CSV (UTF-8, header row first, formula-safe)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([escape_formula(h) for h in table.headers])
        writer.writerows([escape_formula(v) for v in row] for row in table.rows)
    return out


def export_json(report: ResearchReport, out_dir: Path) -> dict[str, Path]:
    """Write ``report.json``, ``facts.csv`` and ``sources.csv`` into ``out_dir``."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / "report.json"
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return {
        "json": json_path,
        "facts_csv": write_csv(facts_table(report), out / "facts.csv"),
        "sources_csv": write_csv(sources_table(report), out / "sources.csv"),
    }
