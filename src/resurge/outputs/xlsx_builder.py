"""Excel workbook builder (openpyxl): a cover sheet plus one styled sheet per shared table.

Every data sheet gets a bold white-on-navy header, frozen panes, an autofilter,
content-based column widths, wrapped text and clickable links; status, confidence
and risk columns get conditional formatting, so the colours stay correct if an
analyst edits the values later.

Injection safety: all text is untrusted, so every string cell is written with
the string data type (openpyxl would otherwise turn "=..." into a formula and
"#REF!" into an error), formula-like text additionally gets Excel's quote prefix
(it stays text even when edited), and hyperlinks are http(s) only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.formatting.rule import CellIsRule, ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from resurge.outputs.charts import CELL_STATUS, FACT_STATUS, PRIORITY_FILL, RISK_BAND_FILL
from resurge.outputs.tables import (
    Cell,
    Table,
    build_tables,
    fmt_datetime,
    is_formula_like,
    key_metrics,
    one_line,
    report_title,
    safe_url,
    target_name,
)
from resurge.schemas import ResearchReport

NAVY, TEAL = "1F2A44", "14B8A6"
HEADER_FILL = PatternFill("solid", start_color=NAVY, end_color=NAVY)
HEADER_FONT = Font(bold=True, color="FFFFFF")
LINK_FONT = Font(color="1D4ED8", underline="single")
WRAP = Alignment(wrap_text=True, vertical="top")
HEADER_ALIGN = Alignment(wrap_text=True, vertical="center")
MAX_CELL_CHARS = 32_000  # Excel's hard limit is 32,767
WIDTH_CAPS = {"long": 70, "url": 48, "urls": 48}
METRIC_KEYS = (
    "competitors",
    "facts_usable",
    "verification_rate",
    "grounding_rate",
    "coverage",
    "avg_qa_score",
    "quality_score",
    "llm_calls",
    "tokens",
    "cache_hits",
    "cost_avoided",
    "duration",
)


def _fill(hex_color: str) -> PatternFill:
    color = hex_color.lstrip("#").upper()
    return PatternFill("solid", start_color=color, end_color=color)


def _safe_value(value: Cell) -> Cell | None:
    if isinstance(value, str):
        text = ILLEGAL_CHARACTERS_RE.sub("", value)
        if len(text) > MAX_CELL_CHARS:
            text = text[: MAX_CELL_CHARS - 1] + "…"
        return text or None
    return value


def _set(ws: Worksheet, row: int, col: int, value: Cell | None) -> Any:
    """Write a value; text is always stored as inert text, never a formula."""
    cell = ws.cell(row=row, column=col)
    cell.value = _safe_value(value) if value is not None else None
    if isinstance(cell.value, str):
        cell.data_type = "s"
        if is_formula_like(cell.value):
            cell.quotePrefix = True
    return cell


def _display_len(value: Cell | None) -> int:
    if value is None:
        return 0
    if isinstance(value, float):
        return len(f"{value:,.2f}")
    return max((len(line) for line in str(value).split("\n")), default=0)


def _write_table(ws: Worksheet, table: Table, start_row: int = 1) -> int:
    """Write header + rows starting at ``start_row``; returns the last row written."""
    for j, header in enumerate(table.headers, start=1):
        cell = _set(ws, start_row, j, header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = HEADER_ALIGN
    for i, row in enumerate(table.rows, start=start_row + 1):
        for j, (header, value) in enumerate(zip(table.headers, row), start=1):
            cell = _set(ws, i, j, value)
            cell.alignment = WRAP
            kind = table.kind(header)
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            if kind == "percent" and numeric:
                cell.number_format = "0%"
            elif kind == "usd" and numeric:
                cell.number_format = '"$"#,##0.00'
            elif kind == "int" and numeric:
                cell.number_format = "#,##0"
            elif kind in ("url", "urls") and isinstance(value, str) and value.strip():
                link = safe_url(value.split()[0])
                if link:
                    cell.hyperlink = link
                    cell.font = LINK_FONT
    last_row = start_row + len(table.rows)
    ws.row_dimensions[start_row].height = 30
    return last_row


def _set_widths(ws: Worksheet, table: Table) -> None:
    for j, header in enumerate(table.headers, start=1):
        cap = WIDTH_CAPS.get(table.kind(header), 40)
        longest = max([len(header) + 2, *(_display_len(r[j - 1]) for r in table.rows[:500])])
        ws.column_dimensions[get_column_letter(j)].width = max(9, min(cap, longest + 2))


def _column_range(table: Table, header: str, first_row: int, last_row: int) -> str | None:
    if header not in table.headers or last_row <= first_row:
        return None
    letter = get_column_letter(table.headers.index(header) + 1)
    return f"{letter}{first_row + 1}:{letter}{last_row}"


def _equals_rules(ws: Worksheet, ref: str | None, fills: dict[str, str]) -> None:
    if not ref:
        return
    for value, color in fills.items():
        ws.conditional_formatting.add(ref, CellIsRule(operator="equal", formula=[f'"{value}"'], fill=_fill(color)))


def _scale_rule(ws: Worksheet, ref: str | None, low: float, mid: float, high: float, reverse: bool = False) -> None:
    if not ref:
        return
    colors = ["F87171", "FDE68A", "4ADE80"]
    if reverse:
        colors.reverse()
    ws.conditional_formatting.add(
        ref,
        ColorScaleRule(
            start_type="num", start_value=low, start_color=colors[0],
            mid_type="num", mid_value=mid, mid_color=colors[1],
            end_type="num", end_value=high, end_color=colors[2],
        ),
    )


def _conditional_formats(ws: Worksheet, table: Table, first_row: int, last_row: int) -> None:
    """Table-specific conditional formatting (status fills, colour scales)."""
    if table.name == "feature_matrix":
        fills = {status: style.fill for status, style in CELL_STATUS.items()}
        for header in table.headers:
            if table.kind(header) == "status":
                _equals_rules(ws, _column_range(table, header, first_row, last_row), fills)
    elif table.name == "facts":
        _equals_rules(ws, _column_range(table, "Status", first_row, last_row),
                      {status: style.fill for status, style in FACT_STATUS.items()})
        _scale_rule(ws, _column_range(table, "Confidence", first_row, last_row), 0, 0.6, 1)
    elif table.name == "risks":
        _scale_rule(ws, _column_range(table, "Score", first_row, last_row), 1, 9, 25, reverse=True)
        _equals_rules(ws, _column_range(table, "Band", first_row, last_row), RISK_BAND_FILL)
    elif table.name == "recommendations":
        _equals_rules(ws, _column_range(table, "Priority", first_row, last_row), PRIORITY_FILL)
    elif table.name == "competitors":
        _scale_rule(ws, _column_range(table, "Threat level (1-5)", first_row, last_row), 1, 3, 5, reverse=True)
    elif table.name == "sources":
        _scale_rule(ws, _column_range(table, "Reliability", first_row, last_row), 0, 0.5, 1)
    elif table.name == "swot":
        _scale_rule(ws, _column_range(table, "Confidence", first_row, last_row), 0, 0.6, 1)


def _data_sheet(ws: Worksheet, table: Table) -> None:
    last_row = _write_table(ws, table)
    _set_widths(ws, table)
    ws.freeze_panes = "B2" if len(table.headers) >= 4 else "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(table.headers))}{max(1, last_row)}"
    _conditional_formats(ws, table, 1, last_row)
    ws.sheet_properties.tabColor = TEAL


def _summary_sheet(ws: Worksheet, report: ResearchReport, table: Table) -> None:
    """Cover sheet: title, headline, key metrics block, then the summary table."""
    ws.title = table.title
    ws.sheet_properties.tabColor = NAVY
    ws.sheet_view.showGridLines = False
    for letter, width in zip("ABCD", (26, 24, 80, 52)):
        ws.column_dimensions[letter].width = width
    brief = report.brief
    title = _set(ws, 1, 1, report_title(report))
    title.font = Font(bold=True, size=18, color=NAVY)
    ws.row_dimensions[1].height = 30
    meta = [f"Target: {target_name(report)}", one_line(brief.industry), one_line(brief.region),
            f"Generated {fmt_datetime(report.created_at)}", f"Run {report.run_id}"]
    _set(ws, 2, 1, " · ".join(m for m in meta if m)).font = Font(color="64748B")
    row = 4
    es = report.executive_summary
    if es.headline:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        cell = _set(ws, row, 1, es.headline)
        cell.font = Font(bold=True, size=13, color=NAVY)
        cell.alignment = WRAP
        ws.row_dimensions[row].height = 20 * max(1, -(-len(es.headline) // 150))
        row += 1
    if es.summary:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        cell = _set(ws, row, 1, es.summary)
        cell.alignment = WRAP
        ws.row_dimensions[row].height = 16 * max(1, -(-len(es.summary) // 170))
        row += 1
    row += 1
    _set(ws, row, 1, "KEY METRICS").font = Font(bold=True, color=TEAL)
    row += 1
    lookup = {m.key: m for m in key_metrics(report)}
    for key in METRIC_KEYS:
        metric = lookup.get(key)
        if metric is None:
            continue
        _set(ws, row, 1, metric.label).font = Font(bold=True, color="334155")
        _set(ws, row, 2, metric.display).font = Font(bold=True, size=12, color=NAVY)
        _set(ws, row, 3, metric.note).font = Font(color="64748B")
        row += 1
    row += 1
    start = row
    last_row = _write_table(ws, table, start_row=start)
    ws.auto_filter.ref = f"A{start}:{get_column_letter(len(table.headers))}{last_row}"


def build_xlsx(report: ResearchReport, path: Path) -> Path:
    """Write the analyst workbook (Summary first, then one sheet per table)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tables = build_tables(report)
    workbook = Workbook()
    _summary_sheet(workbook.active, report, tables["summary"])
    for name, table in tables.items():
        if name != "summary":
            _data_sheet(workbook.create_sheet(table.title[:31]), table)
    workbook.properties.title = report_title(report)[:250]
    workbook.properties.creator = "Resurge"
    workbook.save(out)
    return out
