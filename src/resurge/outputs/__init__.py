"""Design & Communications department: every deliverable rendered from a ``ResearchReport``.

``publish_all`` builds the requested formats into one directory. Each builder
runs in isolation: a failure is logged and recorded as ``"<fmt>_error"`` while
the remaining formats are still produced. Builder modules are imported lazily,
so importing this package is cheap and a missing optional dependency only
affects its own format. (``notify`` is imported eagerly: the package exports a
function with the same name as its submodule, and a lazy submodule import
would rebind the package attribute to the module.)
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from resurge.outputs.notify import notify
from resurge.schemas import ResearchReport

log = logging.getLogger(__name__)

ALL_FORMATS: tuple[str, ...] = ("charts", "site", "html", "md", "pptx", "xlsx", "json", "gsheets")
DEFAULT_FORMATS: frozenset[str] = frozenset(f for f in ALL_FORMATS if f != "gsheets")
_NEEDS_CHARTS = {"charts", "site", "html", "md", "pptx"}

FILE_NAMES: dict[str, str] = {
    "html": "report.html",
    "md": "report.md",
    "pptx": "report.pptx",
    "xlsx": "report.xlsx",
}


def build_charts(report: ResearchReport, out_dir: Path) -> dict[str, Path]:
    """Render every supported chart as ``<out_dir>/<name>.png``."""
    from resurge.outputs.charts import build_charts as impl

    return impl(report, out_dir)


def build_pptx(report: ResearchReport, path: Path, charts: Mapping[str, Path | str] | None = None) -> Path:
    """Write the 16:9 consulting-style deck."""
    from resurge.outputs.pptx_builder import build_pptx as impl

    return impl(report, path, charts)


def build_xlsx(report: ResearchReport, path: Path) -> Path:
    """Write the analyst workbook (one sheet per shared table)."""
    from resurge.outputs.xlsx_builder import build_xlsx as impl

    return impl(report, path)


def build_html(report: ResearchReport, path: Path, charts: Mapping[str, Path | str] | None = None) -> Path:
    """Write the self-contained offline HTML report."""
    from resurge.outputs.html_report import build_html as impl

    return impl(report, path, charts)


def build_site(report: ResearchReport, out_dir: Path, charts: Mapping[str, Path | str] | None = None,
               downloads: Mapping[str, str] | None = None) -> Path:
    """Write the multi-page report site (index + one page per topic and competitor); returns ``index.html``."""
    from resurge.outputs.site import build_site as impl
    return impl(report, out_dir, charts, downloads)


def build_markdown(report: ResearchReport, path: Path, charts: Mapping[str, Path | str] | None = None) -> Path:
    """Write the GitHub-flavoured Markdown report."""
    from resurge.outputs.markdown import build_markdown as impl

    return impl(report, path, charts)


def export_json(report: ResearchReport, out_dir: Path) -> dict[str, Path]:
    """Write ``report.json``, ``facts.csv`` and ``sources.csv``."""
    from resurge.outputs.json_export import export_json as impl

    return impl(report, out_dir)


def publish_gsheets(report: ResearchReport) -> str:
    """Publish the tables to a new Google Sheet; returns its URL (needs service-account env vars)."""
    from resurge.outputs.gsheets import publish_gsheets as impl

    return impl(report)


def _normalise(formats: Iterable[str] | None) -> tuple[list[str], list[str]]:
    """(known formats in canonical order, unknown names)."""
    wanted = set(DEFAULT_FORMATS) if formats is None else {str(f).strip().lower() for f in formats}
    aliases = {"markdown": "md", "excel": "xlsx", "ppt": "pptx", "sheets": "gsheets", "csv": "json"}
    wanted = {aliases.get(f, f) for f in wanted if f}
    return [f for f in ALL_FORMATS if f in wanted], sorted(wanted - set(ALL_FORMATS))


def publish_all(
    report: ResearchReport, out_dir: Path, formats: set[str] | None = None
) -> dict[str, Path | str]:
    """Build the requested deliverables into ``out_dir``.

    ``formats`` ⊆ {"charts", "html", "md", "pptx", "xlsx", "json", "gsheets"};
    ``None`` means everything except "gsheets". Returns ``{format: path_or_url}``
    (plus ``facts_csv``/``sources_csv`` for "json"); a failed builder is logged
    and reported as ``{"<fmt>_error": "<message>"}`` without stopping the rest.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    selected, unknown = _normalise(formats)
    results: dict[str, Path | str] = {}
    for name in unknown:
        log.warning("Unknown output format %r ignored (expected one of %s)", name, ", ".join(ALL_FORMATS))
        results[f"{name}_error"] = f"unknown format {name!r}"

    charts: dict[str, Path] = {}

    def run(fmt: str, action: Callable[[], None]) -> None:
        try:
            action()
        except Exception as exc:  # isolate every builder
            log.warning("Building %s failed: %s", fmt, exc, exc_info=log.isEnabledFor(logging.DEBUG))
            results[f"{fmt}_error"] = f"{type(exc).__name__}: {exc}"

    if _NEEDS_CHARTS & set(selected):

        def make_charts() -> None:
            charts.update(build_charts(report, out / "charts"))
            if "charts" in selected:
                results["charts"] = out / "charts"

        run("charts", make_charts)

    def make_json() -> None:
        paths = export_json(report, out)
        results["json"] = paths["json"]
        results["facts_csv"] = paths["facts_csv"]
        results["sources_csv"] = paths["sources_csv"]

    def make_gsheets() -> None:
        results["gsheets"] = publish_gsheets(report)

    file_builders: dict[str, Callable[[Path], Path]] = {
        "md": lambda path: build_markdown(report, path, charts),
        "html": lambda path: build_html(report, path, charts),
        "xlsx": lambda path: build_xlsx(report, path),
        "pptx": lambda path: build_pptx(report, path, charts),
    }

    def make_file(fmt: str) -> Callable[[], None]:
        def action() -> None:
            results[fmt] = file_builders[fmt](out / FILE_NAMES[fmt])

        return action

    for fmt in ("json", "md", "html", "xlsx", "pptx", "gsheets"):
        if fmt in selected:
            run(fmt, make_json if fmt == "json" else make_gsheets if fmt == "gsheets" else make_file(fmt))

    def make_site() -> None:  # last, so it can link the other deliverables
        labels = {"pptx": "Slide deck (.pptx)", "xlsx": "Workbook (.xlsx)", "html": "One-page print version (.html)",
                  "md": "Markdown (.md)", "facts_csv": "All facts (.csv)", "json": "Raw data (.json)"}
        downloads = {label: f"../{Path(str(results[k])).name}" for k, label in labels.items()
                     if k in results and Path(str(results[k])).is_file()}
        results["site"] = build_site(report, out / "site", charts, downloads)

    if "site" in selected:
        run("site", make_site)
    return results


__all__ = [
    "ALL_FORMATS",
    "DEFAULT_FORMATS",
    "build_charts",
    "build_html",
    "build_site",
    "build_markdown",
    "build_pptx",
    "build_xlsx",
    "export_json",
    "notify",
    "publish_all",
    "publish_gsheets",
]
