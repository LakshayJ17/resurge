"""Optional Google Sheets publisher (gspread + a Google Cloud service account).

Configuration (environment variables):

* ``GOOGLE_SERVICE_ACCOUNT_FILE``: path to the service-account JSON key (required)
* ``GOOGLE_SHEETS_SHARE_WITH``: e-mail address to share the sheet with as writer
* ``GOOGLE_DRIVE_FOLDER_ID``: Drive folder to create the spreadsheet in

The spreadsheet gets one worksheet per shared table from ``tables.build_tables``.
Values are written with ``valueInputOption=RAW`` only, so Sheets stores every
string as literal text and never parses untrusted content as a formula.
It is written with a constant number of API calls regardless of size: create,
one ``batch_update`` that adds every worksheet and applies all formatting
(header style, frozen header row, filters, column widths), one
``values_batch_update`` with every table's values, and an optional share.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from resurge.outputs.tables import Cell, Table, build_tables, one_line, target_name
from resurge.schemas import ResearchReport

ENV_KEY_FILE = "GOOGLE_SERVICE_ACCOUNT_FILE"
ENV_SHARE_WITH = "GOOGLE_SHEETS_SHARE_WITH"
ENV_FOLDER_ID = "GOOGLE_DRIVE_FOLDER_ID"
MAX_CELL_CHARS = 49_000  # Google Sheets limit is 50,000 characters per cell
VALUE_INPUT_OPTION = "RAW"  # never USER_ENTERED: untrusted text must not be parsed as formulas
FIRST_EXTRA_SHEET_ID = 1000

SETUP_HELP = f"""Google Sheets publishing is not configured.
1. In Google Cloud, enable the Google Sheets API and Google Drive API for a project.
2. Create a service account, add a JSON key and download it.
3. export {ENV_KEY_FILE}=/path/to/service-account.json
4. Optional: export {ENV_SHARE_WITH}=you@example.com  (shares the sheet with you as editor)
5. Optional: export {ENV_FOLDER_ID}=<drive folder id>  (share that folder with the service account e-mail)
6. pip install 'resurge[gsheets]'  (gspread + google-auth)"""

_NAVY = {"red": 31 / 255, "green": 42 / 255, "blue": 68 / 255}
_WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_TEAL = {"red": 20 / 255, "green": 184 / 255, "blue": 166 / 255}


@dataclass(frozen=True)
class SheetsConfig:
    """Resolved Google Sheets settings."""

    key_file: Path
    share_with: str | None = None
    folder_id: str | None = None


def load_config(env: Mapping[str, str] | None = None) -> SheetsConfig:
    """Read settings from the environment; raise ``RuntimeError`` with setup steps if missing."""
    env = os.environ if env is None else env
    key = (env.get(ENV_KEY_FILE) or "").strip()
    if not key:
        raise RuntimeError(SETUP_HELP)
    path = Path(key).expanduser()
    if not path.is_file():
        raise RuntimeError(f"{ENV_KEY_FILE} points to {path}, which does not exist.\n\n{SETUP_HELP}")
    return SheetsConfig(
        key_file=path,
        share_with=(env.get(ENV_SHARE_WITH) or "").strip() or None,
        folder_id=(env.get(ENV_FOLDER_ID) or "").strip() or None,
    )


def spreadsheet_title(report: ResearchReport) -> str:
    """"Resurge - <title or target> - YYYY-MM-DD"."""
    name = one_line(report.title) or target_name(report)
    return f"Resurge - {name} - {report.created_at.date().isoformat()}"


def a1_sheet(title: str) -> str:
    """Quote a worksheet title for A1 notation."""
    return "'" + title.replace("'", "''") + "'"


def _cell(value: Cell) -> Cell:
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[: MAX_CELL_CHARS - 3] + "..."
    return value


def _pixel_width(table: Table, column: int) -> int:
    header = table.headers[column]
    longest = max([len(header), *(len(str(r[column]).split("\n")[0]) for r in table.rows[:300])])
    cap = 420 if table.kind(header) in ("long", "url", "urls") else 260
    return max(70, min(cap, 7 * longest + 24))


def build_requests(
    tables: Mapping[str, Table], first_sheet_id: int = 0
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Spreadsheet ``batch_update`` requests and ``values_batch_update`` ranges for ``tables``.

    The first table reuses the spreadsheet's default sheet (``first_sheet_id``);
    every other table gets a new sheet with an explicit, collision-free id.
    """
    requests: list[dict[str, Any]] = []
    data: list[dict[str, Any]] = []
    for index, table in enumerate(tables.values()):
        sheet_id = first_sheet_id if index == 0 else FIRST_EXTRA_SHEET_ID + index
        n_rows = len(table.rows) + 1
        n_cols = max(1, len(table.headers))
        properties = {
            "sheetId": sheet_id,
            "title": table.title[:100],
            "index": index,
            "gridProperties": {"rowCount": max(n_rows, 2), "columnCount": n_cols, "frozenRowCount": 1},
            "tabColor": _NAVY if index == 0 else _TEAL,
        }
        if index == 0:
            requests.append({
                "updateSheetProperties": {
                    "properties": properties,
                    "fields": "title,index,tabColor,gridProperties(rowCount,columnCount,frozenRowCount)",
                }
            })
        else:
            requests.append({"addSheet": {"properties": properties}})
        full = {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": n_rows, "startColumnIndex": 0,
                "endColumnIndex": n_cols}
        requests.append({
            "repeatCell": {
                "range": {**full, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _NAVY,
                    "textFormat": {"bold": True, "foregroundColor": _WHITE},
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                }},
                "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment,wrapStrategy)",
            }
        })
        if n_rows > 1:
            requests.append({
                "repeatCell": {
                    "range": {**full, "startRowIndex": 1},
                    "cell": {"userEnteredFormat": {"verticalAlignment": "TOP", "wrapStrategy": "WRAP"}},
                    "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)",
                }
            })
        requests.append({"setBasicFilter": {"filter": {"range": full}}})
        for column in range(len(table.headers)):
            requests.append({
                "updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": column,
                              "endIndex": column + 1},
                    "properties": {"pixelSize": _pixel_width(table, column)},
                    "fields": "pixelSize",
                }
            })
        values = [list(table.headers)] + [[_cell(v) for v in row] for row in table.rows]
        data.append({"range": f"{a1_sheet(table.title[:100])}!A1", "values": values})
    return requests, data


def publish_gsheets(report: ResearchReport) -> str:
    """Create a Google Sheet with one worksheet per table; returns its URL."""
    config = load_config()
    try:
        import gspread
    except ImportError as exc:  # optional dependency
        raise RuntimeError("gspread is not installed: pip install 'resurge[gsheets]'") from exc
    client = gspread.service_account(filename=str(config.key_file))
    spreadsheet = client.create(spreadsheet_title(report), folder_id=config.folder_id)
    requests, data = build_requests(build_tables(report), first_sheet_id=spreadsheet.sheet1.id)
    spreadsheet.batch_update({"requests": requests})
    spreadsheet.values_batch_update({"valueInputOption": VALUE_INPUT_OPTION, "data": data})
    if config.share_with:
        spreadsheet.share(config.share_with, perm_type="user", role="writer", notify=True,
                          email_message="Your Resurge competitive-intelligence report is ready.")
    return spreadsheet.url
