"""Charts for every deliverable: 200 dpi PNGs drawn with matplotlib's Agg canvas.

Visual system (shared by the deck, PDF and HTML via the constants below):

* Each company gets one identity colour, assigned in a fixed order from a
  colour-blind-validated categorical palette; the colour follows the company
  into every chart. The target company is always brand navy and is drawn
  bolder (diamond marker, bold label) so it never relies on colour alone.
* Status-like encodings (feature support, risk band, verification outcome)
  use soft tints paired with a glyph or a word.
* Thin marks, hairline grids, direct value labels; long labels wrap/truncate.

Matplotlib is imported lazily, so importing this module for its palette is
cheap, and charts never touch global ``pyplot`` state.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from resurge.outputs.tables import (
    IMPACT_LABELS,
    LIKELIHOOD_LABELS,
    RISK_CATEGORY_LABELS,
    THREAT_LABELS,
    clamp_int,
    clip,
    compact_number,
    entity_names,
    fact_index,
    fmt_int,
    fmt_usd,
    is_target,
    matrix_axes,
    one_line,
    order_dimensions,
    pct,
    pretty_dimension,
    risk_band,
    risk_labels,
    risk_score,
    sorted_risks,
    target_name,
    threat_label,
    verification_stats,
    wrap_label,
    wrap_lines,
)
from resurge.outputs.stats import (
    SOURCE_TYPE_LABELS,
    evidence_by_company,
    feature_counts,
    numbered_recommendations,
    voice_of_customer,
)
from resurge.schemas import PricingTier, ResearchReport

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

log = logging.getLogger(__name__)

DPI = 200

# --------------------------------------------------------------------------- #
# Visual identity
# --------------------------------------------------------------------------- #
NAVY = "#1F2A44"
TEAL = "#14B8A6"
AMBER = "#F59E0B"
RED = "#EF4444"
GREEN = "#22C55E"
WHITE = "#FFFFFF"
SLATE: dict[int, str] = {
    50: "#F8FAFC",
    100: "#F1F5F9",
    200: "#E2E8F0",
    300: "#CBD5E1",
    400: "#94A3B8",
    500: "#64748B",
    600: "#475569",
    700: "#334155",
    800: "#1E293B",
    900: "#0F172A",
}
TEAL_RAMP: dict[int, str] = {
    50: "#F0FDFA",
    100: "#CCFBF1",
    200: "#99F6E4",
    300: "#5EEAD4",
    400: "#2DD4BF",
    500: "#14B8A6",
    600: "#0D9488",
    700: "#0F766E",
    800: "#115E59",
    900: "#134E4A",
}
INK, INK_2, INK_3 = SLATE[900], SLATE[600], SLATE[500]
GRID, AXIS = SLATE[200], SLATE[300]

# Validated categorical order (adjacent-pair CVD ΔE ≥ 9 under protan/deutan
# simulation; first three slots pass all-pairs). Never cycled: companies past
# the eighth fold into OTHER_COLOR.
COMPETITOR_PALETTE: tuple[str, ...] = (
    "#2A78D6",  # blue
    "#EB6834",  # orange
    "#1BAF7A",  # aqua
    "#EDA100",  # yellow
    "#E87BA4",  # magenta
    "#008300",  # green
    "#6250D6",  # violet
    "#E34948",  # red
)
TARGET_COLOR = NAVY
OTHER_COLOR = SLATE[400]


@dataclass(frozen=True)
class StatusStyle:
    """Soft fill + readable ink + glyph + word for a status value."""

    fill: str
    ink: str
    glyph: str
    label: str


CELL_STATUS: dict[str, StatusStyle] = {
    "yes": StatusStyle("#BBF7D0", "#14532D", "✓", "Yes"),
    "partial": StatusStyle("#FDE68A", "#78350F", "◐", "Partial"),
    "no": StatusStyle("#FECACA", "#7F1D1D", "✗", "No"),
    "unknown": StatusStyle("#E2E8F0", "#334155", "?", "Unknown"),
}
FACT_STATUS: dict[str, StatusStyle] = {
    "supported": StatusStyle("#DCFCE7", "#166534", "✓", "Supported"),
    "partial": StatusStyle("#FEF3C7", "#92400E", "◐", "Partial"),
    "pending": StatusStyle("#F1F5F9", "#475569", "…", "Pending"),
    "unsupported": StatusStyle("#FEE2E2", "#991B1B", "✗", "Unsupported"),
    "contradicted": StatusStyle("#FEE2E2", "#991B1B", "≠", "Contradicted"),
    "rejected": StatusStyle("#FEE2E2", "#991B1B", "✗", "Rejected"),
}
RISK_BAND_FILL: dict[str, str] = {"Low": "#BBF7D0", "Medium": "#FDE68A", "High": "#FDBA74", "Critical": "#FCA5A5"}
THREAT_FILL: dict[int, str] = {1: "#DCFCE7", 2: "#DCFCE7", 3: "#FEF3C7", 4: "#FEE2E2", 5: "#FECACA"}
PRIORITY_FILL: dict[str, str] = {"high": "#FEE2E2", "medium": "#FEF3C7", "low": "#E2E8F0"}


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """"#1F2A44" -> (31, 42, 68)."""
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def tint(color: str, amount: float) -> str:
    """Mix ``color`` towards white by ``amount`` (0 = unchanged, 1 = white)."""
    r, g, b = hex_to_rgb(color)
    mix = [round(c + (255 - c) * amount) for c in (r, g, b)]
    return "#{:02X}{:02X}{:02X}".format(*mix)


def luminance(color: str) -> float:
    """WCAG relative luminance."""

    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.04045 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = hex_to_rgb(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def ink_on(fill: str) -> str:
    """Near-black or white text, whichever contrasts better with ``fill``."""
    lum = luminance(fill)
    return WHITE if (1.05 / (lum + 0.05)) > ((lum + 0.05) / 0.05) else INK


def entity_colors(report: ResearchReport) -> dict[str, str]:
    """Case-folded company name -> identity colour (target is always navy)."""
    colors: dict[str, str] = {}
    slot = 0
    for name in entity_names(report):
        key = name.casefold()
        if key in colors:
            continue
        if is_target(name, report):
            colors[key] = TARGET_COLOR
        else:
            colors[key] = COMPETITOR_PALETTE[slot] if slot < len(COMPETITOR_PALETTE) else OTHER_COLOR
            slot += 1
    return colors


def color_for(colors: dict[str, str], name: str) -> str:
    """Identity colour of a company (grey when unknown)."""
    return colors.get(one_line(name).casefold(), OTHER_COLOR)


# --------------------------------------------------------------------------- #
# Matplotlib plumbing
# --------------------------------------------------------------------------- #
PAD = 0.2  # outer padding, inches
_FONT_PREFERENCE = ("Helvetica Neue", "Helvetica", "Arial", "Inter", "Liberation Sans", "Noto Sans")


@lru_cache(maxsize=1)
def _matplotlib() -> Any:
    import matplotlib

    matplotlib.use("Agg", force=True)
    return matplotlib


@lru_cache(maxsize=1)
def _font_family() -> tuple[str, ...]:
    _matplotlib()
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    primary = next((name for name in _FONT_PREFERENCE if name in available), None)
    # DejaVu Sans ships with matplotlib and backs up glyphs (✓ ◐ ✗ ◆ →).
    return (primary, "DejaVu Sans") if primary else ("DejaVu Sans",)


@contextmanager
def _style() -> Iterator[None]:
    mpl = _matplotlib()
    rc = {
        "text.parse_math": False,  # labels come from the web: "$12 to $8" is text, "$\\foo$" must not crash a chart
        "font.family": list(_font_family()),
        "font.size": 10.5,
        "text.color": INK,
        "axes.edgecolor": AXIS,
        "axes.labelcolor": INK_2,
        "axes.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.facecolor": WHITE,
        "figure.facecolor": WHITE,
        "savefig.facecolor": WHITE,
        "xtick.color": INK_3,
        "ytick.color": INK_3,
        "xtick.labelcolor": INK_2,
        "ytick.labelcolor": INK,
        "xtick.major.size": 0,
        "ytick.major.size": 0,
        "xtick.labelsize": 9.5,
        "ytick.labelsize": 10.5,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "legend.frameon": False,
        "axes.unicode_minus": False,
    }
    with mpl.rc_context(rc):
        yield


def _text_w(text: str, size: float, bold: bool = False) -> float:
    """Approximate rendered width (inches) of the longest line of ``text``."""
    longest = max((len(line) for line in str(text).split("\n")), default=0)
    return longest * size * (0.6 if bold else 0.56) / 72


def _chars_fit(width_in: float, size: float) -> int:
    return max(10, int(width_in * 72 / (size * 0.56)))


@dataclass
class _Frame:
    """Figure skeleton: width plus header/footnote text and their heights."""

    width: float
    title: str
    subtitle: list[str] = field(default_factory=list)
    footnote: list[str] = field(default_factory=list)

    @property
    def top(self) -> float:
        return PAD + 0.3 + 0.2 * len(self.subtitle) + 0.25

    @property
    def bottom(self) -> float:
        return PAD + (0.17 * len(self.footnote) + 0.15 if self.footnote else 0.0)


def _frame(width: float, title: str, subtitle: str = "", footnote: str = "") -> _Frame:
    usable = width - 2 * PAD
    return _Frame(
        width=width,
        title=clip(title, _chars_fit(usable, 14)),
        subtitle=wrap_lines(subtitle, _chars_fit(usable, 10), 2),
        footnote=wrap_lines(footnote, _chars_fit(usable, 8.5), 3),
    )


def _figure(frame: _Frame, body_height: float) -> Figure:
    """New Agg-backed figure with the frame's title, subtitle and footnote drawn."""
    _matplotlib()
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    height = frame.top + body_height + frame.bottom
    fig = Figure(figsize=(frame.width, height), dpi=DPI, facecolor=WHITE)
    FigureCanvasAgg(fig)
    x = PAD / frame.width
    fig.text(x, 1 - PAD / height, frame.title, ha="left", va="top", fontsize=14, fontweight="bold", color=INK)
    if frame.subtitle:
        fig.text(
            x,
            1 - (PAD + 0.34) / height,
            "\n".join(frame.subtitle),
            ha="left",
            va="top",
            fontsize=10,
            color=INK_3,
            linespacing=1.4,
        )
    if frame.footnote:
        fig.text(
            x,
            PAD / height,
            "\n".join(frame.footnote),
            ha="left",
            va="bottom",
            fontsize=8.5,
            color=INK_3,
            linespacing=1.35,
        )
    return fig


def _axes(fig: Figure, left: float, bottom: float, width: float, height: float, **kwargs: Any) -> Axes:
    """Add axes positioned in inches from the figure's bottom-left corner."""
    fw, fh = fig.get_size_inches()
    return fig.add_axes((left / fw, bottom / fh, width / fw, height / fh), **kwargs)


def _hide_spines(ax: Axes, *names: str) -> None:
    for name in names or ("top", "right", "bottom", "left"):
        ax.spines[name].set_visible(False)


def _cell(ax: Axes, x: float, y: float, w: float, h: float, color: str, aspect: float, gap: float = 0.04) -> None:
    """Rounded heatmap cell with a surface gap (``aspect`` = x-unit / y-unit in inches)."""
    from matplotlib.patches import FancyBboxPatch

    gap_y = gap * aspect
    ax.add_patch(
        FancyBboxPatch(
            (x + gap, y + gap_y),
            w - 2 * gap,
            h - 2 * gap_y,
            boxstyle="round,pad=0,rounding_size=0.05",
            mutation_aspect=aspect,
            facecolor=color,
            edgecolor="none",
        )
    )


def _save(fig: Figure, path: Path) -> Path:
    fig.savefig(path, dpi=DPI, facecolor=WHITE, bbox_inches="tight", pad_inches=0.12, metadata={"Software": "Resurge"})
    return path


def _usd_axis(value: float, _pos: object = None) -> str:
    return f"${value:,.0f}"


def _compact_axis(value: float, _pos: object = None) -> str:
    return compact_number(value) if value else "0"


def _fit_xmax(max_value: float, axis_w_in: float, label_w_in: float) -> float:
    """Upper x-limit leaving room for a direct label after the longest bar."""
    room = max(0.35, axis_w_in - label_w_in)
    return max_value * axis_w_in / room if max_value > 0 else 1.0


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #
@dataclass
class _PriceRow:
    name: str
    entry: PricingTier | None
    top: PricingTier | None
    has_free: bool
    unpriced: list[PricingTier]


_CUSTOM_WORDS = ("contact", "custom", "quote", "sales", "talk", "call")
MAX_PRICING_ROWS = 12


def _pricing_rows(report: ResearchReport) -> list[_PriceRow]:
    rows: list[_PriceRow] = []
    for profile in report.profiles:
        if not profile.pricing:
            continue
        paid = [t for t in profile.pricing if t.price_monthly_usd is not None and t.price_monthly_usd > 0]
        rows.append(
            _PriceRow(
                name=one_line(profile.name) or "Unnamed",
                entry=min(paid, key=lambda t: t.price_monthly_usd or 0) if paid else None,
                top=max(paid, key=lambda t: t.price_monthly_usd or 0) if paid else None,
                has_free=any(t.price_monthly_usd is not None and t.price_monthly_usd <= 0 for t in profile.pricing),
                unpriced=[t for t in profile.pricing if t.price_monthly_usd is None],
            )
        )
    return rows


def _unpriced_note(tier: PricingTier) -> str:
    text = f"{tier.price} {tier.billing or ''}".lower()
    kind = "contact sales" if any(word in text for word in _CUSTOM_WORDS) else "price not listed"
    return f"{clip(tier.name, 16)}: {kind}"


def chart_pricing(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Entry vs top paid tier per competitor (horizontal bars, USD / month)."""
    rows = [r for r in _pricing_rows(report) if r.top or r.unpriced or r.has_free]
    if not any(r.top for r in rows):
        return None
    rows.sort(key=lambda r: -(r.top.price_monthly_usd or 0) if r.top else 1.0)
    hidden = max(0, len(rows) - MAX_PRICING_ROWS)
    rows = rows[:MAX_PRICING_ROWS]
    n = len(rows)
    names = [wrap_label(clip(r.name, 40), 20, 2) for r in rows]
    notes = []
    for r in rows:
        lines = ["Free plan available"] if r.has_free else []
        lines += [_unpriced_note(t) for t in r.unpriced[:2]]
        if len(r.unpriced) > 2:
            lines.append(f"+{len(r.unpriced) - 2} more unpriced tiers")
        notes.append(lines)

    def tip(tier: PricingTier) -> str:
        unit = "/seat" if tier.per_seat else (" flat" if tier.per_seat is False else "")
        return f"{fmt_usd(tier.price_monthly_usd)}{unit} · {clip(tier.name, 18)}"

    tips = [tip(t) for r in rows for t in (r.entry, r.top) if t]
    width = 9.2
    label_w = max(_text_w(nm, 10.5) for nm in names) + 0.3
    notes_w = min(2.9, max((_text_w(line, 9) for ls in notes for line in ls), default=0) + 0.45) if any(notes) else 0.2
    axis_w = width - PAD - label_w - notes_w - PAD
    frame = _frame(
        width,
        "Pricing ladder",
        "Cheapest and most expensive paid tier per competitor · list price in USD per month",
        "Prices normalised to USD per month from vendor pricing pages. Tiers without a public price are listed at right."
        + (f" {hidden} more competitors are listed in the full report." if hidden else ""),
    )
    slot, axis_band = 0.82, 0.45
    fig = _figure(frame, slot * n + axis_band)
    ax = _axes(fig, PAD + label_w, frame.bottom + axis_band, axis_w, slot * n)
    max_price = max(r.top.price_monthly_usd or 0 for r in rows if r.top)
    label_w_in = max(_text_w(t, 9.5) for t in tips) + 0.12
    ax.set_xlim(0, _fit_xmax(max_price, axis_w, label_w_in))
    ax.set_ylim(n - 0.5, -0.5)

    for i, r in enumerate(rows):
        color = color_for(colors, r.name)
        bars: list[tuple[float, PricingTier, str, float]] = []
        if r.entry and r.top and r.entry is not r.top:
            bars = [(i - 0.19, r.entry, tint(color, 0.55), 0.32), (i + 0.19, r.top, color, 0.32)]
        elif r.top:
            bars = [(i, r.top, color, 0.36)]
        for y, tier, fill, height in bars:
            price = tier.price_monthly_usd or 0
            ax.barh(y, price, height=height, color=fill, edgecolor="none", zorder=2)
            ax.annotate(
                tip(tier),
                (price, y),
                xytext=(5, 0),
                textcoords="offset points",
                ha="left",
                va="center",
                fontsize=9.5,
                color=INK_2,
                annotation_clip=False,
            )
        if not bars:
            ax.text(0, i, "  No public list price", va="center", fontsize=9.5, color=INK_3, style="italic")
        if notes[i]:
            ax.text(
                1.0 + 0.3 / axis_w,
                i,
                "\n".join(notes[i]),
                transform=ax.get_yaxis_transform(),
                ha="left",
                va="center",
                fontsize=9,
                color=INK_2,
                linespacing=1.35,
            )
    if any(notes):
        ax.text(
            1.0 + 0.3 / axis_w,
            1.0,
            "Also offered",
            transform=ax.transAxes,
            ha="left",
            va="bottom",
            fontsize=9,
            fontweight="bold",
            color=INK_2,
        )

    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    ax.set_yticks(range(n), labels=names)
    for label, r in zip(ax.get_yticklabels(), rows):
        if is_target(r.name, report):
            label.set_fontweight("bold")
    ax.tick_params(axis="y", pad=8)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=6, integer=True))
    ax.xaxis.set_major_formatter(FuncFormatter(_usd_axis))
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    _hide_spines(ax, "left", "top", "right")
    fig.legend(
        handles=[
            Patch(facecolor=tint(SLATE[600], 0.55), edgecolor="none"),
            Patch(facecolor=SLATE[600], edgecolor="none"),
        ],
        labels=["Cheapest paid tier", "Most expensive tier"],
        loc="upper right",
        bbox_to_anchor=(1 - PAD / width, 1 - (PAD - 0.02) / fig.get_size_inches()[1]),
        ncol=2,
        fontsize=9.5,
        handlelength=1.2,
        handleheight=0.9,
        columnspacing=1.4,
        borderaxespad=0,
    )
    return fig


# --------------------------------------------------------------------------- #
# Feature matrix
# --------------------------------------------------------------------------- #
MAX_MATRIX_FEATURES = 28
MAX_MATRIX_COMPANIES = 8


def chart_feature_matrix(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Feature x company heatmap with yes / partial / no / unknown cells."""
    from matplotlib.patches import Patch, Rectangle

    features, companies, cells = matrix_axes(report)
    if not features or not companies:
        return None
    hidden_f = max(0, len(features) - MAX_MATRIX_FEATURES)
    hidden_c = max(0, len(companies) - MAX_MATRIX_COMPANIES)
    features, companies = features[:MAX_MATRIX_FEATURES], companies[:MAX_MATRIX_COMPANIES]
    nf, nc = len(features), len(companies)
    row_h = 0.46
    cell_w = 1.6 if nc <= 4 else 1.35 if nc <= 6 else 1.15
    labels = [wrap_label(clip(f, 80), 32, 2) for f in features]
    label_w = max(_text_w(lbl, 10) for lbl in labels) + 0.25
    headers = []
    for company in companies:
        name = wrap_label(clip(company, 34), 15, 2)
        if is_target(company, report) and "\n" not in name:
            name += "\n(target)"
        headers.append(name)
    name_lines = max(h.count("\n") + 1 for h in headers)
    line_units = 10.5 * 1.15 / 72 / row_h  # one header text line, in row units
    name_base = 0.56  # bottom of the company name, in row units above the first row
    strip_bottom = name_base + name_lines * line_units + 0.14
    head_h, legend_h = (strip_bottom + 0.13 + 0.12) * row_h, 0.5
    width = max(8.5, PAD + label_w + nc * cell_w + PAD + 0.2)
    hidden = []
    if hidden_f:
        hidden.append(f"{hidden_f} more features")
    if hidden_c:
        hidden.append(f"{hidden_c} more companies")
    frame = _frame(
        width,
        "Feature comparison",
        f"{nf} capabilities across {nc} companies · from product pages, docs and verified facts",
        f"Not shown: {' and '.join(hidden)} (see the full report)." if hidden else "",
    )
    fig = _figure(frame, head_h + nf * row_h + legend_h)
    left = PAD + label_w + max(0.0, (width - 2 * PAD - label_w - nc * cell_w) / 2 - 0.1)
    ax = _axes(fig, left, frame.bottom + legend_h, nc * cell_w, head_h + nf * row_h)
    head_units = head_h / row_h
    ax.set_xlim(0, nc)
    ax.set_ylim(nf, -head_units)
    ax.axis("off")
    aspect = cell_w / row_h

    for j, (company, header) in enumerate(zip(companies, headers)):
        statuses = [(cells.get((f, company)).status if cells.get((f, company)) else "unknown") for f in features]
        ax.add_patch(
            Rectangle((j + 0.3, -strip_bottom - 0.13), 0.4, 0.13, facecolor=color_for(colors, company),
                      edgecolor="none")
        )
        ax.text(
            j + 0.5,
            -name_base,
            header,
            ha="center",
            va="bottom",
            fontsize=10.5,
            fontweight="bold",
            color=INK,
            linespacing=1.15,
        )
        ax.text(
            j + 0.5,
            -0.14,
            f"{statuses.count('yes')} yes · {statuses.count('partial')} partial",
            ha="center",
            va="bottom",
            fontsize=8.5,
            color=INK_3,
        )
        for i, status in enumerate(statuses):
            style = CELL_STATUS.get(status, CELL_STATUS["unknown"])
            _cell(ax, j, i, 1, 1, style.fill, aspect)
            ax.text(j + 0.5, i + 0.5, f"{style.glyph}  {style.label}", ha="center", va="center", fontsize=9.5,
                    color=style.ink)
    for i, label in enumerate(labels):
        ax.text(-0.06, i + 0.5, label, ha="right", va="center", fontsize=10, color=INK, linespacing=1.15)

    fw, fh = fig.get_size_inches()
    fig.legend(
        handles=[Patch(facecolor=s.fill, edgecolor="none") for s in CELL_STATUS.values()],
        labels=[f"{s.glyph} {s.label}" for s in CELL_STATUS.values()],
        loc="lower left",
        bbox_to_anchor=(left / fw, (frame.bottom + 0.06) / fh),
        ncol=4,
        fontsize=9.5,
        handlelength=1.6,
        handleheight=1.1,
        columnspacing=1.6,
        borderaxespad=0,
    )
    return fig


# --------------------------------------------------------------------------- #
# Positioning map
# --------------------------------------------------------------------------- #
def _overlap(a: Any, b: Any) -> float:
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.y1, b.y1) - max(a.y0, b.y0)
    return w * h if w > 0 and h > 0 else 0.0


_EXTRA_DIRECTIONS = [
    (1, 0.5, "left", "bottom"),
    (1, -0.5, "left", "top"),
    (-1, 0.5, "right", "bottom"),
    (-1, -0.5, "right", "top"),
    (0.5, 1, "left", "bottom"),
    (-0.5, 1, "right", "bottom"),
    (0.5, -1, "left", "top"),
    (-0.5, -1, "right", "top"),
]


def _place_labels(
    fig: Figure,
    ax: Axes,
    items: list[tuple[float, float, str, float, dict[str, Any]]],
    soft: list[Any] | None = None,
) -> None:
    """Greedy collision-avoiding placement of point labels.

    Tries 8 directions at growing distances and keeps the first spot that
    overlaps nothing (``soft`` obstacles such as reference lines count less);
    labels pushed away from their point get a thin leader line.
    """
    from matplotlib.transforms import Bbox

    renderer = fig.canvas.get_renderer()  # type: ignore[attr-defined]
    frame = ax.get_window_extent(renderer)
    px_per_pt = fig.dpi / 72
    taken = []
    for x, y, _text, radius, _style in items:
        cx, cy = ax.transData.transform((x, y))
        r = radius * px_per_pt
        taken.append(Bbox.from_extents(cx - r, cy - r, cx + r, cy + r))
    soft = soft or []
    directions = [
        (1, 0, "left", "center"),
        (-1, 0, "right", "center"),
        (1, 1, "left", "bottom"),
        (1, -1, "left", "top"),
        (-1, 1, "right", "bottom"),
        (-1, -1, "right", "top"),
        (0, 1, "center", "bottom"),
        (0, -1, "center", "top"),
    ]
    for x, y, text, radius, style in items:
        best: tuple[float, Any, Any, float, tuple[float, float, str, str]] | None = None
        distances = (radius + 5, radius + 16, radius + 30, radius + 46, radius + 64)
        for level, distance in enumerate(distances):
            for dx, dy, ha, va in directions if level < 2 else directions + _EXTRA_DIRECTIONS:
                ann = ax.annotate(
                    text, (x, y), xytext=(dx * distance, dy * distance), textcoords="offset points",
                    ha=ha, va=va, annotation_clip=False, **style,
                )
                box = ann.get_window_extent(renderer).padded(3 * px_per_pt)
                outside = box.width * box.height - _overlap(box, frame)
                cost = (
                    sum(_overlap(box, t) for t in taken)
                    + 3 * sum(_overlap(box, s) for s in soft)
                    + 4 * outside
                )
                if best is None or cost < best[0]:
                    if best is not None:
                        best[1].remove()
                    best = (cost, ann, box, distance, (dx, dy, ha, va))
                else:
                    ann.remove()
                if cost == 0:
                    break
            if best is not None and best[0] == 0:
                break
        assert best is not None
        _cost, ann, box, distance, (dx, dy, ha, va) = best
        if distance > distances[0]:
            ann.remove()
            ax.annotate(
                text, (x, y), xytext=(dx * distance, dy * distance), textcoords="offset points", ha=ha, va=va,
                annotation_clip=False,
                arrowprops={"arrowstyle": "-", "color": SLATE[400], "lw": 0.8, "shrinkA": 0, "shrinkB": radius},
                **style,
            )
        taken.append(box)


def chart_positioning(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """2x2 positioning map (0..10 axes, quadrant lines at 5, target highlighted)."""
    pm = report.positioning
    points = [p for p in pm.points if one_line(p.competitor)]
    if len(points) < 2:
        return None
    width, plot, left, right, below = 8.4, 6.0, 1.05, 0.45, 0.8
    target = target_name(report)
    frame = _frame(width, "Competitive positioning", f"Analyst-scored 0–10 on both axes · ◆ = {target} (target)")
    fig = _figure(frame, plot + below)
    ax = _axes(fig, left, frame.bottom + below, width - left - right, plot)
    ax.set_xlim(-0.4, 10.4)
    ax.set_ylim(-0.4, 10.4)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_color(AXIS)
    ax.axvline(5, color=AXIS, linewidth=1, zorder=1)
    ax.axhline(5, color=AXIS, linewidth=1, zorder=1)
    ax.set_xlabel(clip(pm.x_axis, 70), fontsize=10.5, color=INK_2, labelpad=20)
    ax.set_ylabel(clip(pm.y_axis, 70), fontsize=10.5, color=INK_2, labelpad=20)
    end_style = {"fontsize": 9, "color": INK_3}
    ax.text(0, -0.02, f"← {clip(pm.x_low, 24)}", transform=ax.transAxes, ha="left", va="top", **end_style)
    ax.text(1, -0.02, f"{clip(pm.x_high, 24)} →", transform=ax.transAxes, ha="right", va="top", **end_style)
    ax.text(-0.02, 0, f"← {clip(pm.y_low, 24)}", transform=ax.transAxes, ha="right", va="bottom", rotation=90,
            **end_style)
    ax.text(-0.02, 1, f"{clip(pm.y_high, 24)} →", transform=ax.transAxes, ha="right", va="top", rotation=90,
            **end_style)

    items: list[tuple[float, float, str, float, dict[str, Any]]] = []
    for p in points:
        x = max(0.0, min(10.0, float(p.x)))
        y = max(0.0, min(10.0, float(p.y)))
        target_point = p.is_target or is_target(p.competitor, report)
        color = TARGET_COLOR if target_point else color_for(colors, p.competitor)
        size = 330 if target_point else 200
        ax.scatter([x], [y], s=size, marker="D" if target_point else "o", color=color, edgecolors=WHITE,
                   linewidths=2, zorder=6)
        style = {
            "fontsize": 10.5 if target_point else 10,
            "fontweight": "bold" if target_point else "normal",
            "color": INK,
            "zorder": 5,
            "bbox": {"boxstyle": "round,pad=0.2", "facecolor": WHITE, "edgecolor": "none", "alpha": 0.85},
        }
        items.append((x, y, clip(p.competitor, 26), math.sqrt(size) / 2 + 2, style))
    from matplotlib.transforms import Bbox

    x0, y0 = ax.transData.transform((-0.4, -0.4))
    x1, y1 = ax.transData.transform((10.4, 10.4))
    mx, my = ax.transData.transform((5, 5))
    quadrant_lines = [Bbox.from_extents(x0, my - 2, x1, my + 2), Bbox.from_extents(mx - 2, y0, mx + 2, y1)]
    _place_labels(fig, ax, items, soft=quadrant_lines)
    return fig


# --------------------------------------------------------------------------- #
# Risk heatmap
# --------------------------------------------------------------------------- #
def chart_risk_heatmap(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """5x5 likelihood x impact grid with risk markers and a ranked legend list."""
    from matplotlib.patches import Circle, Patch, Rectangle

    risks = sorted_risks(report)
    if not risks:
        return None
    labels = risk_labels(report)
    width, grid, left, gap, below, legend_h = 11.6, 4.4, 1.3, 0.65, 0.95, 0.45
    list_w = width - left - grid - gap - PAD
    frame = _frame(width, "Risk heatmap", "Likelihood × impact on 1–5 scales · number in each cell = score · "
                   "markers = risks, ranked at right")
    fig = _figure(frame, grid + below + legend_h)
    base = frame.bottom + legend_h + below
    ax = _axes(fig, left, base, grid, grid)
    ax.set_xlim(0.5, 5.5)
    ax.set_ylim(0.5, 5.5)
    ax.set_aspect("equal", adjustable="box")
    for lk in range(1, 6):
        for im in range(1, 6):
            score = lk * im
            ax.add_patch(Rectangle((lk - 0.47, im - 0.47), 0.94, 0.94, facecolor=RISK_BAND_FILL[risk_band(score)],
                                   edgecolor="none", zorder=1))
            ax.text(lk - 0.39, im + 0.39, str(score), ha="left", va="top", fontsize=7.5, color=SLATE[600],
                    alpha=0.8, zorder=2)

    groups: dict[tuple[int, int], list] = defaultdict(list)
    for risk in risks:
        groups[(clamp_int(risk.likelihood, 1, 5), clamp_int(risk.impact, 1, 5))].append(risk)
    for (lk, im), group in groups.items():
        shown = group if len(group) <= 6 else group[:5]
        marks = [labels[r.id] for r in shown] + ([f"+{len(group) - 5}"] if len(group) > 6 else [])
        cols = min(3, len(marks))
        rows_n = math.ceil(len(marks) / cols)
        spacing = 0.29
        for idx, mark in enumerate(marks):
            row, col = divmod(idx, cols)
            in_row = min(cols, len(marks) - row * cols)
            x = lk + (col - (in_row - 1) / 2) * spacing
            y = im + ((rows_n - 1) / 2 - row) * spacing - 0.04
            ax.add_patch(Circle((x, y), 0.135, facecolor=NAVY, edgecolor=WHITE, linewidth=1.2, zorder=3))
            ax.text(x, y, mark, ha="center", va="center", fontsize=7 if len(mark) <= 2 else 5.8, color=WHITE,
                    fontweight="bold", zorder=4)

    ax.set_xticks(range(1, 6), labels=[f"{n}\n{wrap_label(LIKELIHOOD_LABELS[n], 9, 2)}" for n in range(1, 6)])
    ax.set_yticks(range(1, 6), labels=[f"{IMPACT_LABELS[n]}  {n}" for n in range(1, 6)])
    ax.tick_params(axis="x", labelsize=8.5, labelcolor=INK_2, pad=4)
    ax.tick_params(axis="y", labelsize=8.5, labelcolor=INK_2, pad=4)
    ax.set_xlabel("Likelihood →", fontsize=10, color=INK_2, labelpad=6)
    ax.set_ylabel("Impact →", fontsize=10, color=INK_2, labelpad=6)
    _hide_spines(ax)

    fw, fh = fig.get_size_inches()
    bands = [("Low", "1–4"), ("Medium", "5–9"), ("High", "10–14"), ("Critical", "15–25")]
    fig.legend(
        handles=[Patch(facecolor=RISK_BAND_FILL[b], edgecolor="none") for b, _ in bands],
        labels=[f"{b} {r}" for b, r in bands],
        loc="lower left",
        bbox_to_anchor=(left / fw, (frame.bottom + 0.02) / fh),
        ncol=4,
        fontsize=9,
        handlelength=1.4,
        handleheight=1.0,
        columnspacing=1.3,
        borderaxespad=0,
    )

    lst = _axes(fig, left + grid + gap, base, list_w, grid)
    lst.set_xlim(0, list_w)
    lst.set_ylim(grid, 0)
    lst.axis("off")
    entry_h = 0.53
    capacity = max(1, int(grid / entry_h))
    overflow = len(risks) > capacity
    visible = risks[: capacity - 1] if overflow else risks
    title_chars = _chars_fit(list_w - 0.45, 10)
    lst.text(0, -0.08, "Ranked by score", fontsize=9, fontweight="bold", color=INK_2, va="bottom")
    for k, risk in enumerate(visible):
        y = (k + 0.5) * entry_h
        mark = labels[risk.id]
        lst.add_patch(Circle((0.15, y), 0.135, facecolor=NAVY, edgecolor="none", zorder=3))
        lst.text(0.15, y, mark, ha="center", va="center", fontsize=7 if len(mark) <= 2 else 5.8, color=WHITE,
                 fontweight="bold", zorder=4)
        lst.text(0.42, y - 0.03, clip(risk.title, title_chars), fontsize=10, color=INK, va="bottom")
        score = risk_score(risk)
        meta = (
            f"{RISK_CATEGORY_LABELS.get(risk.category, risk.category)} · "
            f"L{clamp_int(risk.likelihood, 1, 5)} × I{clamp_int(risk.impact, 1, 5)} = {score} ({risk_band(score)})"
        )
        lst.text(0.42, y + 0.03, clip(meta, title_chars + 8), fontsize=8.5, color=INK_3, va="top")
    if overflow:
        lst.text(0.42, (len(visible) + 0.5) * entry_h, f"+{len(risks) - len(visible)} more in the risk register",
                 fontsize=9, color=INK_3, va="center", style="italic")
    return fig


# --------------------------------------------------------------------------- #
# Coverage heatmap
# --------------------------------------------------------------------------- #
def chart_coverage(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Competitor x dimension heatmap of usable-fact counts (0 = research gap)."""
    from matplotlib.colors import LinearSegmentedColormap, to_hex

    profiles = [p for p in report.profiles if p.coverage][:12]
    if not profiles:
        return None
    dims = order_dimensions({d for p in profiles for d in p.coverage}, report)[:14]
    if not dims:
        return None
    nr, nd = len(profiles), len(dims)
    cell_w, row_h, head_h, total_w = 0.92, 0.5, 0.62, 0.8
    names = [clip(p.name, 26) for p in profiles]
    label_w = max(_text_w(n, 10.5) for n in names) + 0.45
    width = max(8.0, PAD + label_w + nd * cell_w + total_w + PAD)
    frame = _frame(
        width,
        "Research coverage",
        "Usable facts (supported or partial) per competitor and research dimension",
        "A 0 marks a research gap: no usable evidence yet. A dash means the dimension was not researched.",
    )
    fig = _figure(frame, head_h + nr * row_h)
    ax = _axes(fig, PAD + label_w, frame.bottom, nd * cell_w + total_w, head_h + nr * row_h)
    total_units = total_w / cell_w
    ax.set_xlim(0, nd + total_units)
    ax.set_ylim(nr, -head_h / row_h)
    ax.axis("off")
    aspect = cell_w / row_h
    values = [v for p in profiles for d in dims if (v := p.coverage.get(d)) is not None]
    vmax = max([1, *values])
    ramp = LinearSegmentedColormap.from_list(
        "coverage", [TEAL_RAMP[100], TEAL_RAMP[300], TEAL_RAMP[500], TEAL_RAMP[700], TEAL_RAMP[900]]
    )
    for j, dim in enumerate(dims):
        ax.text(j + 0.5, -0.14, wrap_label(pretty_dimension(dim), 12, 2), ha="center", va="bottom", fontsize=8.8,
                color=INK_2, linespacing=1.1)
    ax.text(nd + total_units / 2, -0.14, "Total", ha="center", va="bottom", fontsize=8.8, fontweight="bold",
            color=INK_2)
    for i, (profile, name) in enumerate(zip(profiles, names)):
        ax.text(-0.42, i + 0.5, name, ha="right", va="center", fontsize=10.5,
                fontweight="bold" if is_target(profile.name, report) else "normal", color=INK)
        ax.scatter([-0.2], [i + 0.5], s=60, color=color_for(colors, profile.name), edgecolors="none",
                   clip_on=False)
        total = 0
        for j, dim in enumerate(dims):
            value = profile.coverage.get(dim)
            if value is None:
                fill, text, ink = SLATE[50], "–", INK_3
            elif value <= 0:
                fill, text, ink = "#FEF2F2", "0", "#B91C1C"
            else:
                total += value
                fill = to_hex(ramp(0.1 + 0.9 * min(1.0, value / vmax)))
                text, ink = fmt_int(value), ink_on(fill)
            _cell(ax, j, i, 1, 1, fill, aspect, gap=0.035)
            ax.text(j + 0.5, i + 0.5, text, ha="center", va="center", fontsize=10, color=ink,
                    fontweight="bold" if value else "normal")
        ax.text(nd + total_units / 2, i + 0.5, fmt_int(total), ha="center", va="center", fontsize=10.5,
                fontweight="bold", color=INK)
    return fig


# --------------------------------------------------------------------------- #
# Verification funnel
# --------------------------------------------------------------------------- #
def chart_verification_funnel(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Facts extracted -> grounded -> usable, plus the rejected share."""
    stats = verification_stats(report)
    if stats.total <= 0:
        return None
    stages = [
        ("Facts extracted", stats.total, TEAL_RAMP[900]),
        ("Grounded: quote in source", stats.grounded, TEAL_RAMP[700]),
        ("Usable: supported or partial", stats.usable, TEAL_RAMP[500]),
        ("Rejected: unsupported, contradicted or rejected", stats.failed, "#DC2626"),
    ]
    labels = [wrap_label(label, 30, 2) for label, _, _ in stages]
    width = 8.4
    label_w = max(_text_w(lbl, 10.5) for lbl in labels) + 0.3
    value_w = 1.45
    frame = _frame(
        width,
        "Verification funnel",
        f"{stats.usable} of {stats.total} extracted facts are usable ({pct(stats.usable / stats.total)}) · "
        "each fact needs a verbatim source quote and an independent model check",
        f"{stats.pending} fact{'s' if stats.pending != 1 else ''} still pending verification." if stats.pending else "",
    )
    slot = 0.62
    fig = _figure(frame, slot * len(stages) + 0.1)
    ax = _axes(fig, PAD + label_w, frame.bottom + 0.1, width - 2 * PAD - label_w - value_w, slot * len(stages))
    ax.set_xlim(0, stats.total)
    ax.set_ylim(len(stages) - 0.5, -0.5)
    for i, (_, value, color) in enumerate(stages):
        ax.barh(i, value, height=0.56, color=color, edgecolor="none")
        ax.annotate(f"{fmt_int(value)}  ·  {pct(value / stats.total)}", (value, i), xytext=(6, 0),
                    textcoords="offset points", va="center", ha="left", fontsize=10.5, color=INK,
                    annotation_clip=False)
    ax.set_yticks(range(len(stages)), labels=labels)
    ax.tick_params(axis="y", pad=8)
    ax.set_xticks([])
    _hide_spines(ax, "top", "right", "bottom")
    ax.spines["left"].set_color(AXIS)
    return fig


# --------------------------------------------------------------------------- #
# Threat levels
# --------------------------------------------------------------------------- #
def chart_threat_levels(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Analyst threat level (1..5) per competitor."""
    items = [(p.name, clamp_int(p.threat_level, 1, 5)) for p in report.profiles if p.threat_level is not None]
    if not items:
        return None
    items.sort(key=lambda t: (-t[1], one_line(t[0]).lower()))
    n = len(items)
    names = [wrap_label(clip(name, 40), 22, 2) for name, _ in items]
    width = 9.6
    label_w = max(_text_w(nm, 10.5) for nm in names) + 0.3
    value_w = 1.5
    frame = _frame(width, "Threat level by competitor", "Analyst judgement on a 1 (minimal) to 5 (critical) scale")
    slot, axis_band = 0.58, 0.55
    fig = _figure(frame, slot * n + axis_band)
    ax = _axes(fig, PAD + label_w, frame.bottom + axis_band, width - 2 * PAD - label_w - value_w, slot * n)
    ax.set_xlim(0, 5)
    ax.set_ylim(n - 0.5, -0.5)
    for i, (name, level) in enumerate(items):
        ax.barh(i, level, height=0.5, color=color_for(colors, name), edgecolor="none", zorder=2)
        ax.annotate(f"{level}/5 · {threat_label(level)}", (level, i), xytext=(6, 0), textcoords="offset points",
                    va="center", ha="left", fontsize=10, color=INK, annotation_clip=False)
    ax.set_yticks(range(n), labels=names)
    ax.set_xticks(range(1, 6), labels=[f"{k}\n{THREAT_LABELS[k]}" for k in range(1, 6)])
    ax.tick_params(axis="x", labelsize=8.5, labelcolor=INK_3)
    ax.tick_params(axis="y", pad=8)
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    _hide_spines(ax, "left", "top", "right")
    return fig


# --------------------------------------------------------------------------- #
# Model usage
# --------------------------------------------------------------------------- #
def chart_model_usage(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """LLM calls and tokens per model, as two aligned panels (never a dual axis)."""
    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    all_usage = [u for u in report.metrics.model_usage if u.calls or u.tokens_in or u.tokens_out]
    if not all_usage:
        return None
    usage = sorted(all_usage, key=lambda u: (-u.calls, -(u.tokens_in + u.tokens_out)))[:12]
    n = len(usage)
    names = [clip(u.model_id, 36) for u in usage]
    width = 12.0
    label_w = max(_text_w(nm, 10) for nm in names) + 0.3
    gap = 0.55
    panel_w = (width - 2 * PAD - label_w - gap) / 2
    calls = sum(u.calls for u in all_usage)
    tokens = sum(u.tokens_in + u.tokens_out for u in all_usage)
    hidden = len(all_usage) - n
    frame = _frame(
        width,
        "Model usage",
        f"{len(all_usage)} models · {fmt_int(calls)} LLM calls · {compact_number(tokens)} tokens in this run",
        f"{hidden} lower-volume models not shown." if hidden else "",
    )
    slot, title_h, axis_band = 0.5, 0.42, 0.4
    fig = _figure(frame, title_h + slot * n + axis_band)
    bottom = frame.bottom + axis_band
    ax1 = _axes(fig, PAD + label_w, bottom, panel_w, slot * n)
    ax2 = _axes(fig, PAD + label_w + panel_w + gap, bottom, panel_w, slot * n)

    def call_tip(u: Any) -> str:
        extra = []
        if u.cache_hits:
            extra.append(f"{fmt_int(u.cache_hits)} cached")
        if u.failures:
            extra.append(f"{fmt_int(u.failures)} failed")
        return fmt_int(u.calls) + (f" ({', '.join(extra)})" if extra else "")

    call_tips = [call_tip(u) for u in usage]
    token_tips = [compact_number(u.tokens_in + u.tokens_out) for u in usage]
    max_calls = max(u.calls for u in usage)
    max_tokens = max(u.tokens_in + u.tokens_out for u in usage)
    ax1.set_xlim(0, _fit_xmax(max_calls, panel_w, max(_text_w(t, 9.5) for t in call_tips) + 0.12))
    ax2.set_xlim(0, _fit_xmax(max_tokens, panel_w, max(_text_w(t, 9.5) for t in token_tips) + 0.12))
    for ax in (ax1, ax2):
        ax.set_ylim(n - 0.5, -0.5)
        ax.grid(axis="x", zorder=0)
        ax.set_axisbelow(True)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=5, integer=True))
        ax.xaxis.set_major_formatter(FuncFormatter(_compact_axis))
        _hide_spines(ax, "left", "top", "right")
    for i, u in enumerate(usage):
        ax1.barh(i, u.calls, height=0.5, color=NAVY, edgecolor="none", zorder=2)
        ax1.annotate(call_tips[i], (u.calls, i), xytext=(5, 0), textcoords="offset points", va="center",
                     fontsize=9.5, color=INK_2, annotation_clip=False)
        ax2.barh(i, u.tokens_in, height=0.5, color=NAVY, edgecolor=WHITE, linewidth=1, zorder=2)
        ax2.barh(i, u.tokens_out, left=u.tokens_in, height=0.5, color=TEAL, edgecolor=WHITE, linewidth=1,
                 zorder=2)
        ax2.annotate(token_tips[i], (u.tokens_in + u.tokens_out, i), xytext=(5, 0), textcoords="offset points",
                     va="center", fontsize=9.5, color=INK_2, annotation_clip=False)
    ax1.set_yticks(range(n), labels=names)
    ax1.tick_params(axis="y", labelsize=10, pad=8)
    ax2.set_yticks(range(n), labels=[""] * n)
    for ax, title in ((ax1, "LLM calls"), (ax2, "Tokens")):
        ax.text(0, 1 + 0.12 / (slot * n), title, transform=ax.transAxes, ha="left", va="bottom", fontsize=10.5,
                fontweight="bold", color=INK_2)
    ax2.legend(
        handles=[Patch(facecolor=NAVY, edgecolor="none"), Patch(facecolor=TEAL, edgecolor="none")],
        labels=["Input", "Output"],
        loc="lower right",
        bbox_to_anchor=(1.0, 1 + 0.05 / (slot * n)),
        ncol=2,
        fontsize=9,
        handlelength=1.1,
        handleheight=0.9,
        columnspacing=1.0,
        borderaxespad=0,
    )
    return fig


# --------------------------------------------------------------------------- #
# Cost by team size
# --------------------------------------------------------------------------- #
TEAM_SIZES: tuple[int, ...] = (1, 10, 50, 200)
SOURCE_COLORS: dict[str, str] = {
    "official": NAVY, "news": "#2A78D6", "review": TEAL, "community": "#EDA100", "reference": "#6250D6",
    "code": SLATE[600], "social": "#E87BA4", "other": SLATE[300],
}
PRIORITY_COLORS: dict[str, str] = {"high": NAVY, "medium": TEAL_RAMP[600], "low": SLATE[400]}


def _usd_compact(value: float) -> str:
    return f"${compact_number(value)}" if abs(value) >= 1000 else f"${value:,.0f}"


def _spread(values: list[float], gap: float) -> list[float]:
    """Positions for labels at ``values`` (ascending) pushed apart by at least ``gap``."""
    out: list[float] = []
    for v in values:
        out.append(max(v, out[-1] + gap) if out else v)
    return out


def chart_cost_curve(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Annual list price of each company's cheapest paid plan for 1, 10, 50 and 200 seats."""
    from matplotlib.ticker import FuncFormatter

    from resurge.outputs.site import price_summary

    series = []
    for profile in report.profiles:
        entry = price_summary(profile).entry
        if entry and entry.monthly:
            per_seat = entry.per_seat is not False
            ys = [entry.monthly * 12 * (n if per_seat else 1) for n in TEAM_SIZES]
            series.append((profile.name, f"{clip(one_line(profile.name), 22)} · {clip(one_line(entry.tier.name), 16)}",
                           ys, per_seat))
    if not series:
        return None
    width = 9.6
    labels = [f"{label}  {_usd_compact(ys[-1])}" for _, label, ys, _ in series]
    label_w = max(_text_w(lbl, 9.5) for lbl in labels) + 0.3
    flat = any(not per_seat for *_, per_seat in series)
    frame = _frame(width, "Annual cost by team size",
                   "Cheapest paid plan of each company at list price, for a year",
                   "Flat-priced plans cost the same at every team size." if flat else "")
    body, axis_band = 3.7, 0.45
    fig = _figure(frame, body)
    ax = _axes(fig, PAD + 0.7, frame.bottom + axis_band, width - 2 * PAD - 0.7 - label_w, body - axis_band - 0.1)
    xs = list(range(len(TEAM_SIZES)))
    top = max(max(ys) for _, _, ys, _ in series) * 1.08 or 1.0
    ax.set_ylim(0, top)
    ax.set_xlim(-0.15, len(TEAM_SIZES) - 0.85)
    for name, _, ys, _ in series:
        ax.plot(xs, ys, color=color_for(colors, name), linewidth=2.2, marker="o", markersize=5.5, zorder=3)
    ends = sorted(((ys[-1], f"{label}  {_usd_compact(ys[-1])}", name) for name, label, ys, _ in series))
    for (y, text, name), ly in zip(ends, _spread([e[0] for e in ends], top * 0.075), strict=True):
        ax.annotate(text, (xs[-1], y), xytext=(xs[-1] + 0.12, ly), textcoords="data", va="center", ha="left",
                    fontsize=9.5, color=color_for(colors, name) if luminance(color_for(colors, name)) < 0.5 else INK,
                    annotation_clip=False)
    ax.set_xticks(xs, labels=[f"{n} seat{'s' if n != 1 else ''}" for n in TEAM_SIZES])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: _usd_compact(v)))
    ax.grid(axis="y", zorder=0)
    ax.set_axisbelow(True)
    _hide_spines(ax, "top", "right", "left")
    return fig


# --------------------------------------------------------------------------- #
# Competitor moves over time
# --------------------------------------------------------------------------- #
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def chart_timeline(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Dated competitor moves on a time axis, one row per company."""
    from resurge.outputs.site import timeline

    moves = [m for m in timeline(report, fact_index(report)) if m.sort[0]]
    if len(moves) < 2:
        return None

    def when(m: Any) -> float:
        year, month = m.sort
        return year + ((month - 0.5) / 12 if month else 0.5)

    companies = list(dict.fromkeys(m.competitor for m in sorted(moves, key=when, reverse=True)))
    rows = {c: i for i, c in enumerate(companies)}
    xs = [when(m) for m in moves]
    lo, hi = min(xs), max(xs)
    pad = max(hi - lo, 0.5) * 0.06 + 1 / 24
    n = len(companies)
    names = [wrap_label(clip(c, 36), 22, 2) for c in companies]
    width = 9.6
    label_w = max(_text_w(nm, 10.5) for nm in names) + 0.3
    count_w = 1.2
    year_only = any(m.sort[1] == 0 for m in moves)
    frame = _frame(width, "Competitor moves over time",
                   "Launches, pricing changes, funding and other news with a known date",
                   "Hollow dots: only the year is known." if year_only else "")
    slot, axis_band = 0.55, 0.5
    fig = _figure(frame, slot * n + axis_band)
    ax = _axes(fig, PAD + label_w, frame.bottom + axis_band, width - 2 * PAD - label_w - count_w, slot * n)
    ax.set_xlim(lo - pad, hi + pad)
    ax.set_ylim(n - 0.5, -0.5)
    for i in range(n):
        ax.axhline(i, color=GRID, linewidth=0.8, zorder=1)
    stacked: Counter[tuple[int, float]] = Counter()
    for m in moves:
        i, x = rows[m.competitor], when(m)
        k = stacked[(i, round(x, 3))]
        stacked[(i, round(x, 3))] += 1
        dy = ((k + 1) // 2) * 0.26 * (1 if k % 2 else -1)
        c = color_for(colors, m.competitor)
        ax.scatter([x], [i + dy], s=75, color=c if m.sort[1] else WHITE, edgecolors=c, linewidths=1.6, zorder=3)
    per = Counter(m.competitor for m in moves)
    for c, i in rows.items():
        ax.annotate(f"{per[c]} move{'s' if per[c] != 1 else ''}", (1, i), xycoords=("axes fraction", "data"),
                    xytext=(10, 0), textcoords="offset points", va="center", fontsize=9.5, color=INK_2,
                    annotation_clip=False)
    first, last = math.ceil((lo - pad) * 12), math.floor((hi + pad) * 12)
    step = next(s for s in (1, 2, 3, 6, 12, 24, 60) if (last - first) / s <= 7)
    ticks = [t for t in range(first, last + 1) if t % step == 0]
    fmt = (lambda t: str(t // 12)) if step >= 12 else (lambda t: f"{_MON[t % 12]} {t // 12}")
    ax.set_xticks([t / 12 for t in ticks], labels=[fmt(t) for t in ticks])
    ax.set_yticks(range(n), labels=names)
    ax.tick_params(axis="y", pad=8)
    ax.grid(axis="x", zorder=0)
    ax.set_axisbelow(True)
    _hide_spines(ax, "left", "top", "right")
    return fig


# --------------------------------------------------------------------------- #
# Capability breadth
# --------------------------------------------------------------------------- #
def chart_feature_breadth(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """How many of the compared capabilities each company has, has partly, lacks, or has no evidence for."""
    from matplotlib.patches import Patch

    counts = feature_counts(report)
    if not counts:
        return None
    total = len(report.feature_matrix.features)
    order = ("yes", "partial", "no", "unknown")
    n = len(counts)
    names = [wrap_label(clip(name, 36), 22, 2) for name, _ in counts]
    width = 9.6
    label_w = max(_text_w(nm, 10.5, bold=True) for nm in names) + 0.3
    value_w = 1.55
    frame = _frame(width, "Capabilities confirmed by public evidence",
                   f"Out of {total} capabilities compared · unknown means no public evidence either way")
    slot, legend_h = 0.55, 0.42
    fig = _figure(frame, legend_h + slot * n + 0.05)
    ax = _axes(fig, PAD + label_w, frame.bottom + 0.05, width - 2 * PAD - label_w - value_w, slot * n)
    ax.set_xlim(0, total)
    ax.set_ylim(n - 0.5, -0.5)
    for i, (name, c) in enumerate(counts):
        left = 0
        for key in order:
            value = c[key]
            if not value:
                continue
            style = CELL_STATUS[key]
            ax.barh(i, value, left=left, height=0.56, color=style.fill, edgecolor=WHITE, linewidth=1.5)
            if value / total >= 0.08:
                ax.text(left + value / 2, i, str(value), ha="center", va="center", fontsize=9.5,
                        color=style.ink, fontweight="bold")
            left += value
        ax.annotate(f"{c['yes']} of {total} · {pct(c['yes'] / total)}", (1, i), xycoords=("axes fraction", "data"),
                    xytext=(8, 0), textcoords="offset points", va="center", fontsize=10, color=INK,
                    fontweight="bold" if is_target(name, report) else "normal", annotation_clip=False)
    ax.set_yticks(range(n), labels=names)
    for tick, (name, _) in zip(ax.get_yticklabels(), counts, strict=True):
        if is_target(name, report):
            tick.set_fontweight("bold")
    ax.tick_params(axis="y", pad=8)
    ax.set_xticks([])
    _hide_spines(ax)
    ax.legend(handles=[Patch(facecolor=CELL_STATUS[k].fill, edgecolor="none") for k in order],
              labels=["Has it", "Partly", "Doesn't", "No evidence"], loc="lower left",
              bbox_to_anchor=(0, 1 + 0.06 / (slot * n)), ncol=4, fontsize=9, handlelength=1.1, handleheight=0.9,
              columnspacing=1.2, borderaxespad=0)
    return fig


# --------------------------------------------------------------------------- #
# Voice of the customer
# --------------------------------------------------------------------------- #
PRAISE, COMPLAINT = "#34D399", "#F87171"


def chart_voice(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Praise against complaints found in verified customer sources, per company (diverging bars)."""
    rows = sorted(voice_of_customer(report), key=lambda r: (-(r[1] - r[2]), r[0].lower()))
    if not rows:
        return None
    n = len(rows)
    most = max(max(p, c) for _, p, c, _ in rows) or 1
    names = [wrap_label(clip(name, 36), 22, 2) for name, *_ in rows]
    width = 9.6
    label_w = max(_text_w(nm, 10.5) for nm in names) + 0.3
    score_w = 1.15
    scored = any(score is not None for *_, score in rows)
    frame = _frame(width, "What customers say",
                   "Praise and complaints in verified customer sources such as reviews, forums and app stores",
                   "Score: analyst-rated sentiment from −1 (very negative) to +1 (very positive) where the evidence "
                   "allowed one." if scored else "")
    slot, head = 0.55, 0.38
    fig = _figure(frame, head + slot * n + 0.05)
    ax = _axes(fig, PAD + label_w, frame.bottom + 0.05, width - 2 * PAD - label_w - score_w, slot * n)
    ax.set_xlim(-most * 1.3, most * 1.3)
    ax.set_ylim(n - 0.5, -0.5)
    ax.axvline(0, color=AXIS, linewidth=1, zorder=1)
    for i, (_, praise, complaints, score) in enumerate(rows):
        if complaints:
            ax.barh(i, -complaints, height=0.52, color=COMPLAINT, edgecolor="none", zorder=2)
            ax.annotate(str(complaints), (-complaints, i), xytext=(-6, 0), textcoords="offset points", ha="right",
                        va="center", fontsize=10, color=INK, annotation_clip=False)
        if praise:
            ax.barh(i, praise, height=0.52, color=PRAISE, edgecolor="none", zorder=2)
            ax.annotate(str(praise), (praise, i), xytext=(6, 0), textcoords="offset points", ha="left",
                        va="center", fontsize=10, color=INK, annotation_clip=False)
        if score is not None:
            ax.annotate(f"score {score:+.1f}", (1, i), xycoords=("axes fraction", "data"), xytext=(10, 0),
                        textcoords="offset points", va="center", fontsize=9.5, color=INK_2, annotation_clip=False)
    top = 1 + 0.1 / (slot * n)
    ax.text(0.48, top, "◀ Complaints", transform=ax.transAxes, ha="right", va="bottom", fontsize=10,
            fontweight="bold", color="#B91C1C")
    ax.text(0.52, top, "Praise ▶", transform=ax.transAxes, ha="left", va="bottom", fontsize=10,
            fontweight="bold", color="#047857")
    ax.set_yticks(range(n), labels=names)
    ax.tick_params(axis="y", pad=8)
    ax.set_xticks([])
    _hide_spines(ax)
    return fig


# --------------------------------------------------------------------------- #
# Impact against effort
# --------------------------------------------------------------------------- #
def chart_impact_effort(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Recommended actions on an impact/effort grid; numbers match the action plan."""
    from matplotlib.patches import Patch, Rectangle

    recs = numbered_recommendations(report)
    spots = {(clamp_int(r.effort, 1, 5), clamp_int(r.impact, 1, 5)) for _, r in recs}
    if len(recs) < 2 or len(spots) < 2:
        return None  # every action in one spot says nothing
    width, body = 8.6, 5.1
    frame = _frame(width, "Where to act first",
                   "Each recommended action by expected impact and effort, 1 to 5 · numbers match the action plan")
    fig = _figure(frame, body)
    left, bottom = PAD + 0.85, frame.bottom + 0.62
    ax = _axes(fig, left, bottom, width - left - PAD - 0.1, body - 0.62 - 0.45)
    ax.set_xlim(0.5, 5.5)
    ax.set_ylim(0.5, 5.5)
    zones = ((0.5, 3.5, 2.0, "Quick wins", "#DCFCE7", "#166534"), (2.5, 3.5, 3.0, "Big bets", SLATE[100], INK_2),
             (0.5, 0.5, 2.0, "Fill-ins", SLATE[50], INK_3), (2.5, 0.5, 3.0, "Plan carefully", "#FFFBEB", "#92400E"))
    for x, y, w, label, fill, ink in zones:
        h = 2.0 if y > 1 else 3.0
        ax.add_patch(Rectangle((x, y), w, h, facecolor=fill, edgecolor=WHITE, linewidth=2, zorder=0))
        ax.text(x + 0.1, y + h - 0.1, label, ha="left", va="top", fontsize=10.5, fontweight="bold", color=ink, zorder=1)
    groups: dict[tuple[int, int], list[tuple[int, Any]]] = defaultdict(list)
    for number, rec in recs:
        groups[(clamp_int(rec.effort, 1, 5), clamp_int(rec.impact, 1, 5))].append((number, rec))
    for (x, y), items in groups.items():
        k = len(items)
        radius = 0 if k == 1 else 0.2 if k <= 4 else 0.3
        for j, (number, rec) in enumerate(items):
            angle = math.pi / 2 + 2 * math.pi * j / k
            px, py = x + radius * math.cos(angle), y + radius * math.sin(angle)
            ax.scatter([px], [py], s=330, color=PRIORITY_COLORS.get(rec.priority, NAVY), edgecolors=WHITE,
                       linewidths=1.5, zorder=3)
            ax.text(px, py, str(number), ha="center", va="center", fontsize=9, fontweight="bold", color=WHITE, zorder=4)
    ax.set_xticks(range(1, 6), labels=["1\nlow", "2", "3", "4", "5\nhigh"])
    ax.set_yticks(range(1, 6), labels=["1 low", "2", "3", "4", "5 high"])
    ax.set_xlabel("Effort", fontsize=10.5, color=INK_2, labelpad=6)
    ax.set_ylabel("Impact", fontsize=10.5, color=INK_2, labelpad=6)
    _hide_spines(ax, "top", "right")
    used = [p for p in ("high", "medium", "low") if any(r.priority == p for _, r in recs)]
    ax.legend(handles=[Patch(facecolor=PRIORITY_COLORS[p], edgecolor="none") for p in used],
              labels=[f"{p.title()} priority" for p in used], loc="lower right", bbox_to_anchor=(1.0, 1.01),
              ncol=len(used), fontsize=9, handlelength=1.1, handleheight=0.9, columnspacing=1.2, borderaxespad=0)
    return fig


# --------------------------------------------------------------------------- #
# Evidence mix
# --------------------------------------------------------------------------- #
def chart_evidence_mix(report: ResearchReport, colors: dict[str, str]) -> Figure | None:
    """Verified facts per company, split by the kind of source they came from."""
    from matplotlib.patches import Patch

    rows = evidence_by_company(report)[:10]
    if not rows:
        return None
    kinds = [k for k in SOURCE_COLORS if any(c[k] for _, c in rows)]
    n = len(rows)
    most = max(sum(c.values()) for _, c in rows)
    names = [wrap_label(clip(name, 36), 22, 2) for name, _ in rows]
    width = 9.6
    label_w = max(_text_w(nm, 10.5) for nm in names) + 0.3
    value_w = 0.8
    frame = _frame(width, "Where the evidence comes from", "Verified facts per company, by the type of their source")
    slot, legend_h = 0.52, 0.42
    fig = _figure(frame, legend_h + slot * n + 0.05)
    ax = _axes(fig, PAD + label_w, frame.bottom + 0.05, width - 2 * PAD - label_w - value_w, slot * n)
    ax.set_xlim(0, most)
    ax.set_ylim(n - 0.5, -0.5)
    for i, (_, c) in enumerate(rows):
        left = 0
        for kind in kinds:
            if c[kind]:
                ax.barh(i, c[kind], left=left, height=0.54, color=SOURCE_COLORS[kind], edgecolor=WHITE, linewidth=1.2)
                left += c[kind]
        ax.annotate(fmt_int(left), (left, i), xytext=(6, 0), textcoords="offset points", va="center", fontsize=10,
                    color=INK, annotation_clip=False)
    ax.set_yticks(range(n), labels=names)
    ax.tick_params(axis="y", pad=8)
    ax.set_xticks([])
    _hide_spines(ax, "top", "right", "bottom")
    ax.spines["left"].set_color(AXIS)
    ax.legend(handles=[Patch(facecolor=SOURCE_COLORS[k], edgecolor="none") for k in kinds],
              labels=[SOURCE_TYPE_LABELS[k] for k in kinds], loc="lower left",
              bbox_to_anchor=(0, 1 + 0.06 / (slot * n)), ncol=min(len(kinds), 6), fontsize=9, handlelength=1.1,
              handleheight=0.9, columnspacing=1.1, borderaxespad=0)
    return fig


# --------------------------------------------------------------------------- #
# Registry / entry point
# --------------------------------------------------------------------------- #
ChartBuilder = Callable[[ResearchReport, dict[str, str]], "Figure | None"]

CHARTS: dict[str, ChartBuilder] = {
    "pricing": chart_pricing,
    "feature_matrix": chart_feature_matrix,
    "positioning": chart_positioning,
    "risk_heatmap": chart_risk_heatmap,
    "coverage": chart_coverage,
    "verification_funnel": chart_verification_funnel,
    "threat_levels": chart_threat_levels,
    "model_usage": chart_model_usage,
    "cost_curve": chart_cost_curve,
    "timeline": chart_timeline,
    "feature_breadth": chart_feature_breadth,
    "voice": chart_voice,
    "impact_effort": chart_impact_effort,
    "evidence_mix": chart_evidence_mix,
}
CHART_TITLES: dict[str, str] = {
    "pricing": "Pricing ladder: cheapest and most expensive paid tier per competitor",
    "feature_matrix": "Feature comparison heatmap",
    "positioning": "Competitive positioning map",
    "risk_heatmap": "Risk heatmap (likelihood × impact)",
    "coverage": "Research coverage by competitor and dimension",
    "verification_funnel": "Fact verification funnel",
    "threat_levels": "Threat level by competitor",
    "model_usage": "LLM calls and tokens per model",
    "cost_curve": "Annual cost by team size for each company's cheapest paid plan",
    "timeline": "Competitor moves over time",
    "feature_breadth": "Capabilities confirmed by public evidence, per company",
    "voice": "Customer praise against complaints, per company",
    "impact_effort": "Recommended actions by impact and effort",
    "evidence_mix": "Verified facts per company by type of source",
}


def build_charts(report: ResearchReport, out_dir: Path) -> dict[str, Path]:
    """Render every chart the data supports into ``out_dir`` as ``<name>.png``.

    Charts without enough data are skipped (absent from the result); a chart
    that fails is logged and skipped without affecting the others.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    colors = entity_colors(report)
    produced: dict[str, Path] = {}
    with _style():
        for name, builder in CHARTS.items():
            try:
                fig = builder(report, colors)
                if fig is not None:
                    produced[name] = _save(fig, out / f"{name}.png")
            except Exception as exc:  # one broken chart must not sink the rest
                log.warning("Chart %r failed: %s", name, exc, exc_info=log.isEnabledFor(logging.DEBUG))
    return produced
