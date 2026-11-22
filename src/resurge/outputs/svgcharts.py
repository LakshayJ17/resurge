"""Inline SVG charts for the report site: no scripts, no external files, readable in light and dark mode.

Design rules (shared with the rest of the site):
* Monochrome by default. Marks are drawn in the page ink; the client ("you") is the only accent, so the
  eye goes to the comparison that matters. Identity comes from direct labels, never from a colour key.
* Thin marks: 14px bars with a 4px rounded data end and a square baseline end, 2px lines, 5px dots.
* Recessive axes: hairline gridlines, 12-13px labels in the muted ink, values in the text ink.
* Every mark carries a <title>, so hovering shows its exact value; every chart has a <title>/<desc>
  and sits next to a table with the same numbers.
All text is escaped: labels come from web pages via LLMs.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from markupsafe import Markup, escape

W = 900  # viewBox width = drawn width on desktop, so chart text matches body text (no upscaling)
_ids = itertools.count(1)


def _e(value: object) -> str:
    return str(escape(str(value)))


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _svg(height: float, title: str, desc: str, body: str, cls: str = "", width: int = W) -> Markup:
    n = next(_ids)
    return Markup(
        f'<svg class="chart {cls}" viewBox="0 0 {width} {height:.0f}" role="img" aria-labelledby="ct{n} cd{n}" '
        f'preserveAspectRatio="xMinYMin meet"><title id="ct{n}">{_e(title)}</title><desc id="cd{n}">{_e(desc)}</desc>'
        f"{body}</svg>")


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Horizontal bar, square at the baseline, rounded at the data end."""
    if w <= 0:
        return ""
    r = min(r, w, h / 2)
    return (f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r},{r} 0 0 1 {r},{r}v{h - 2 * r:.1f}a{r},{r} 0 0 1 -{r},{r}"
            f"h-{w - r:.1f}z")


def nice_step(value: float, n: int = 4) -> float:
    """A round tick step (1, 2, 2.5 or 5 x 10^k) giving at most ``n`` intervals up to ``value``."""
    raw = max(value, 1e-9) / n
    exp = 10 ** math.floor(math.log10(raw))
    return next(m * exp for m in (1, 2, 2.5, 5, 10) if m * exp >= raw)


def nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(value))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if m * exp >= value:
            return m * exp
    return 10 * exp


def money(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1e6:.1f}M".replace(".0M", "M")
    if v >= 10_000:
        return f"${v / 1e3:.0f}k"
    if v >= 1_000:
        return f"${v / 1e3:.1f}k".replace(".0k", "k")
    return f"${v:,.2f}".replace(".00", "") if v % 1 else f"${v:,.0f}"


def _note(note: str) -> str:
    return f'<tspan class="sub"> {_e(_clip(note, 28))}</tspan>' if note else ""


# --------------------------------------------------------------------------- #
# Horizontal bars
# --------------------------------------------------------------------------- #
@dataclass
class Bar:
    label: str
    value: float
    tip: str = ""
    highlight: bool = False
    note: str = ""  # shown after the value, muted (e.g. the plan name)


def hbar(bars: Sequence[Bar], *, title: str, desc: str, fmt: Callable[[float], str] = money,
         max_value: float | None = None, label_w: int = 170, ticks: int = 0,
         tick_fmt: Callable[[float], str] | None = None, min_items: int = 2) -> Markup:
    if len(bars) < min_items:  # one bar compares nothing: the table says it better
        return Markup("")
    bar_h, gap, top = 14, 14, 6 if not ticks else 22
    plot_x, plot_w = label_w + 12, W - label_w - 12 - 190
    vmax = max_value or nice_max(max(b.value for b in bars))
    parts = []
    if ticks:
        for k in range(ticks + 1):
            v = vmax * k / ticks
            x = plot_x + plot_w * k / ticks
            parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{top - 6}" '
                         f'y2="{top + len(bars) * (bar_h + gap) - gap + 4}"/>'
                         f'<text class="tick" x="{x:.1f}" y="{top - 10}" text-anchor="middle">{_e((tick_fmt or fmt)(v))}</text>')
    for i, b in enumerate(bars):
        y = top + i * (bar_h + gap)
        w = plot_w * max(0.0, b.value) / vmax if vmax else 0
        cls = "mark hl" if b.highlight else "mark"
        parts.append(
            f'<g><title>{_e(b.tip or f"{b.label}: {fmt(b.value)}")}</title>'
            f'<text class="lbl{" hl-text" if b.highlight else ""}" x="{label_w}" y="{y + bar_h / 2}" '
            f'text-anchor="end" dominant-baseline="central">{_e(_clip(b.label, 26))}</text>'
            f'<path class="{cls}" d="{_bar_path(plot_x, y, max(w, 1.5), bar_h)}"/>'
            f'<text class="val" x="{plot_x + w + 8:.1f}" y="{y + bar_h / 2}" dominant-baseline="central">'
            f'{_e(fmt(b.value))}{_note(b.note)}</text></g>')
    parts.append(f'<line class="axis" x1="{plot_x}" x2="{plot_x}" y1="{top - 4}" y2="{top + len(bars) * (bar_h + gap) - gap + 4}"/>')
    height = top + len(bars) * (bar_h + gap) - gap + 10
    return _svg(height, title, desc, "".join(parts))


# --------------------------------------------------------------------------- #
# Stacked bars (feature coverage)
# --------------------------------------------------------------------------- #
@dataclass
class Stack:
    label: str
    segments: list[tuple[str, int]]  # (kind, count) in legend order
    highlight: bool = False


SEGMENT_NAMES = {"yes": "Confirmed", "partial": "Partial", "no": "Not offered", "unknown": "Not found"}


def stacked(stacks: Sequence[Stack], *, title: str, desc: str, label_w: int = 170) -> Markup:
    if not stacks:
        return Markup("")
    kinds = [k for k, _ in stacks[0].segments]
    bar_h, gap, top = 14, 14, 30
    plot_x, plot_w = label_w + 12, W - label_w - 12 - 150
    total = max(sum(c for _, c in s.segments) for s in stacks) or 1
    parts = []
    lx = plot_x
    for k in kinds:  # legend: swatch + name, in the order the segments are drawn
        parts.append(f'<rect class="seg seg-{k}" x="{lx}" y="4" width="10" height="10" rx="2"/>'
                     f'<text class="tick" x="{lx + 15}" y="9" dominant-baseline="central">{_e(SEGMENT_NAMES.get(k, k))}</text>')
        lx += 22 + 7.2 * len(SEGMENT_NAMES.get(k, k)) + 18
    for i, s in enumerate(stacks):
        y = top + i * (bar_h + gap)
        x = plot_x
        n = sum(c for _, c in s.segments)
        parts.append(f'<text class="lbl{" hl-text" if s.highlight else ""}" x="{label_w}" y="{y + bar_h / 2}" '
                     f'text-anchor="end" dominant-baseline="central">{_e(_clip(s.label, 26))}</text>')
        for k, c in s.segments:
            if not c:
                continue
            w = plot_w * c / total
            parts.append(f'<g><title>{_e(f"{s.label}: {c} of {n} {SEGMENT_NAMES.get(k, k).lower()}")}</title>'
                         f'<rect class="seg seg-{k}" x="{x + 1:.1f}" y="{y}" width="{max(w - 2, 1):.1f}" height="{bar_h}" rx="2"/></g>')
            x += w
        yes = dict(s.segments).get("yes", 0)
        parts.append(f'<text class="val" x="{plot_x + plot_w + 10}" y="{y + bar_h / 2}" dominant-baseline="central">'
                     f'{yes} of {n} confirmed</text>')
    height = top + len(stacks) * (bar_h + gap) - gap + 8
    return _svg(height, title, desc, "".join(parts))


# --------------------------------------------------------------------------- #
# Line chart: annual cost by team size
# --------------------------------------------------------------------------- #
@dataclass
class Series:
    label: str
    points: list[tuple[float, float]]
    highlight: bool = False
    tip: str = ""


def lines(series: Sequence[Series], *, title: str, desc: str, x_label: str, xs: Sequence[float],
          fmt_y: Callable[[float], str] = money) -> Markup:
    if len(series) < 2:
        return Markup("")
    left, right, top, bottom = 64, 200, 14, 40
    pw, ph = W - left - right, 260
    xmax = max(xs)
    ystep = nice_step(max(y for s in series for _, y in s.points))
    ymax = ystep * math.ceil(max(y for s in series for _, y in s.points) / ystep)
    nticks = int(round(ymax / ystep))
    def px(x: float) -> float:
        return left + pw * x / xmax
    def py(y: float) -> float:
        return top + ph - ph * y / ymax
    parts = []
    for k in range(nticks + 1):
        v = ystep * k
        parts.append(f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{py(v):.1f}" y2="{py(v):.1f}"/>'
                     f'<text class="tick" x="{left - 8}" y="{py(v):.1f}" text-anchor="end" dominant-baseline="central">{_e(fmt_y(v))}</text>')
    for x in xs:
        parts.append(f'<text class="tick" x="{px(x):.1f}" y="{top + ph + 18}" text-anchor="middle">{_e(f"{x:g}")}</text>')
    parts.append(f'<text class="tick" x="{left + pw / 2:.1f}" y="{top + ph + 36}" text-anchor="middle">{_e(x_label)}</text>')
    labels: list[tuple[float, Series]] = []
    for s in sorted(series, key=lambda s: s.highlight):  # the client is drawn last, on top
        d = "M" + " L".join(f"{px(x):.1f},{py(y):.1f}" for x, y in s.points)
        cls = "line hl" if s.highlight else "line"
        parts.append(f'<g><title>{_e(s.tip or s.label)}</title><path class="{cls}" d="{d}"/>'
                     + "".join(f'<circle class="{"dot hl" if s.highlight else "dot"}" cx="{px(x):.1f}" cy="{py(y):.1f}" r="3.5">'
                               f'<title>{_e(f"{s.label}: {fmt_y(y)} for {x:g} seats")}</title></circle>' for x, y in s.points)
                     + "</g>")
        labels.append((py(s.points[-1][1]), s))
    labels.sort(key=lambda t: t[0])
    last = -1e9
    for y, s in labels:  # direct labels at the line ends, nudged apart
        y = max(y, last + 15)
        last = y
        parts.append(f'<text class="lbl{" hl-text" if s.highlight else ""}" x="{left + pw + 10}" y="{y:.1f}" '
                     f'dominant-baseline="central">{_e(_clip(s.label, 30))}</text>')
    return _svg(top + ph + bottom + 6, title, desc, "".join(parts))


# --------------------------------------------------------------------------- #
# Scatter: positioning map
# --------------------------------------------------------------------------- #
@dataclass
class Point:
    label: str
    x: float  # 0..10
    y: float  # 0..10
    tip: str = ""
    highlight: bool = False


def scatter(points: Sequence[Point], *, title: str, desc: str, x_axis: str, y_axis: str, x_low: str, x_high: str,
            y_low: str, y_high: str) -> Markup:
    if not points:
        return Markup("")
    left, top, size = 120, 16, 380
    pw = min(W - left - 200, 560)
    def px(v: float) -> float:
        return left + pw * v / 10
    def py(v: float) -> float:
        return top + size - size * v / 10
    parts = [f'<rect class="frame" x="{left}" y="{top}" width="{pw}" height="{size}"/>',
             f'<line class="grid" x1="{px(5)}" x2="{px(5)}" y1="{top}" y2="{top + size}"/>',
             f'<line class="grid" x1="{left}" x2="{left + pw}" y1="{py(5)}" y2="{py(5)}"/>',
             f'<text class="tick" x="{left}" y="{top + size + 18}">{_e(_clip(x_low, 28))}</text>',
             f'<text class="tick" x="{left + pw}" y="{top + size + 18}" text-anchor="end">{_e(_clip(x_high, 28))}</text>',
             f'<text class="axis-name" x="{left + pw / 2}" y="{top + size + 38}" text-anchor="middle">{_e(_clip(x_axis, 60))}</text>',
             f'<text class="tick" x="{left - 10}" y="{top + size}" text-anchor="end">{_e(_clip(y_low, 16))}</text>',
             f'<text class="tick" x="{left - 10}" y="{top + 10}" text-anchor="end">{_e(_clip(y_high, 16))}</text>',
             f'<text class="axis-name" transform="translate({left - 96},{top + size / 2}) rotate(-90)" '
             f'text-anchor="middle">{_e(_clip(y_axis, 46))}</text>']
    placed: list[tuple[float, float]] = []
    for p in sorted(points, key=lambda p: p.highlight):
        x, y = px(max(0, min(10, p.x))), py(max(0, min(10, p.y)))
        ly = y
        while any(abs(ly - qy) < 15 and abs(x - qx) < 120 for qx, qy in placed):
            ly += 15
        placed.append((x, ly))
        cls = "dot hl" if p.highlight else "dot"
        parts.append(f'<g><title>{_e(p.tip or p.label)}</title><circle class="{cls}" cx="{x:.1f}" cy="{y:.1f}" r="6"/>'
                     f'<text class="lbl{" hl-text" if p.highlight else ""}" x="{x + 11:.1f}" y="{ly:.1f}" '
                     f'dominant-baseline="central">{_e(_clip(p.label, 22))}</text></g>')
    return _svg(top + size + 48, title, desc, "".join(parts))


# --------------------------------------------------------------------------- #
# Timeline: dated moves, one lane per company
# --------------------------------------------------------------------------- #
@dataclass
class Event:
    lane: str
    when: tuple[int, int]  # (year, month); month 0 = year only
    tip: str
    highlight: bool = False


def timeline(events: Sequence[Event], *, title: str, desc: str, label_w: int = 140) -> Markup:
    dated = [e for e in events if e.when[0]]
    if not dated:
        return Markup("")
    def idx(w: tuple[int, int]) -> int:
        return w[0] * 12 + max(w[1], 1) - 1
    lo, hi = min(idx(e.when) for e in dated), max(idx(e.when) for e in dated)
    lo, hi = lo - 1, hi + 1
    lanes = list(dict.fromkeys(e.lane for e in dated))
    lane_h, top = 30, 8
    plot_x, plot_w = label_w + 16, W - label_w - 16 - 20
    def px(i: float) -> float:
        return plot_x + plot_w * (i - lo) / max(1, hi - lo)
    parts = []
    span = hi - lo
    step = 1 if span <= 12 else 2 if span <= 24 else 3 if span <= 36 else 6 if span <= 72 else 12
    month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    bottom_y = top + len(lanes) * lane_h
    for i in range(lo, hi + 1):
        if (i - lo) % step:
            continue
        x = px(i)
        y, m = divmod(i, 12)
        parts.append(f'<line class="grid" x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{bottom_y}"/>'
                     f'<text class="tick" x="{x:.1f}" y="{bottom_y + 16}" text-anchor="middle">{month[m]} {str(y)[2:]}</text>')
    for li, lane in enumerate(lanes):
        y = top + li * lane_h + lane_h / 2
        hl = any(e.highlight for e in dated if e.lane == lane)
        parts.append(f'<line class="lane" x1="{plot_x}" x2="{plot_x + plot_w}" y1="{y}" y2="{y}"/>'
                     f'<text class="lbl{" hl-text" if hl else ""}" x="{label_w}" y="{y}" text-anchor="end" '
                     f'dominant-baseline="central">{_e(_clip(lane, 20))}</text>')
        seen: dict[int, int] = {}
        for e in [e for e in dated if e.lane == lane]:
            i = idx(e.when)
            k = seen.get(i, 0)
            seen[i] = k + 1
            x = px(i) + (k * 9 - 4 if k else 0)
            parts.append(f'<circle class="{"dot hl" if e.highlight else "dot"}" cx="{x:.1f}" cy="{y}" r="5">'
                         f'<title>{_e(e.tip)}</title></circle>')
    return _svg(bottom_y + 26, title, desc, "".join(parts))


# --------------------------------------------------------------------------- #
# Risk matrix: likelihood x impact
# --------------------------------------------------------------------------- #
@dataclass
class RiskMark:
    n: int
    likelihood: int
    impact: int
    tip: str


def risk_matrix(marks: Sequence[RiskMark], *, title: str, desc: str) -> Markup:
    if not marks:
        return Markup("")
    cell, left, top = 64, 64, 8
    parts = []
    for imp in range(1, 6):
        for lik in range(1, 6):
            x, y = left + (lik - 1) * cell, top + (5 - imp) * cell
            level = min(4, (lik * imp - 1) // 5)  # 0..4, a single-hue ordinal ramp
            parts.append(f'<rect class="cell l{level}" x="{x + 1}" y="{y + 1}" width="{cell - 2}" height="{cell - 2}" rx="2"/>')
        parts.append(f'<text class="tick" x="{left - 10}" y="{top + (5 - imp) * cell + cell / 2}" text-anchor="end" '
                     f'dominant-baseline="central">{imp}</text>')
    for lik in range(1, 6):
        parts.append(f'<text class="tick" x="{left + (lik - 1) * cell + cell / 2}" y="{top + 5 * cell + 16}" '
                     f'text-anchor="middle">{lik}</text>')
    parts.append(f'<text class="axis-name" x="{left + 2.5 * cell}" y="{top + 5 * cell + 36}" text-anchor="middle">Likelihood</text>'
                 f'<text class="axis-name" transform="translate({left - 40},{top + 2.5 * cell}) rotate(-90)" text-anchor="middle">Impact</text>')
    slots: dict[tuple[int, int], int] = {}
    for m in marks:
        key = (max(1, min(5, m.likelihood)), max(1, min(5, m.impact)))
        k = slots.get(key, 0)
        slots[key] = k + 1
        cx = left + (key[0] - 1) * cell + 19 + (k % 2) * 26
        cy = top + (5 - key[1]) * cell + 19 + (k // 2) * 26
        parts.append(f'<g><title>{_e(m.tip)}</title><circle class="pin" cx="{cx}" cy="{cy}" r="10"/>'
                     f'<text class="pin-n" x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="central">{m.n}</text></g>')
    return _svg(top + 5 * cell + 46, title, desc, "".join(parts), cls="narrow", width=left + 5 * cell + 16)


# --------------------------------------------------------------------------- #
# Diverging bars: two opposing counts per row (complaints | praise)
# --------------------------------------------------------------------------- #
@dataclass
class Diverge:
    label: str
    left: int
    right: int
    tip: str = ""
    highlight: bool = False


def diverging(rows: Sequence[Diverge], *, title: str, desc: str, left_name: str, right_name: str,
              label_w: int = 170) -> Markup:
    if not rows:
        return Markup("")
    bar_h, gap, top = 14, 14, 26
    plot_x = label_w + 12
    plot_w = W - plot_x - 40
    mid = plot_x + plot_w / 2
    half = plot_w / 2 - 40
    vmax = max(max(r.left, r.right) for r in rows) or 1
    parts = [f'<text class="tick" x="{mid - 10:.1f}" y="{top - 12}" text-anchor="end">◀ {_e(left_name)}</text>',
             f'<text class="tick" x="{mid + 10:.1f}" y="{top - 12}">{_e(right_name)} ▶</text>']
    for i, r in enumerate(rows):
        y = top + i * (bar_h + gap)
        wl, wr = half * r.left / vmax, half * r.right / vmax
        cy = y + bar_h / 2
        bars = ""
        if r.left:
            bars += (f'<path class="mark neg" transform="translate({2 * mid:.1f},0) scale(-1,1)" '
                     f'd="{_bar_path(mid, y, wl, bar_h)}"/>'
                     f'<text class="val" x="{mid - wl - 8:.1f}" y="{cy}" text-anchor="end" '
                     f'dominant-baseline="central">{r.left}</text>')
        if r.right:
            bars += (f'<path class="mark{" hl" if r.highlight else ""}" d="{_bar_path(mid, y, wr, bar_h)}"/>'
                     f'<text class="val" x="{mid + wr + 8:.1f}" y="{cy}" dominant-baseline="central">{r.right}</text>')
        parts.append(f'<g><title>{_e(r.tip or f"{r.label}: {r.left} {left_name.lower()}, {r.right} {right_name.lower()}")}</title>'
                     f'<text class="lbl{" hl-text" if r.highlight else ""}" x="{label_w}" y="{cy}" text-anchor="end" '
                     f'dominant-baseline="central">{_e(_clip(r.label, 26))}</text>{bars}</g>')
    bottom = top + len(rows) * (bar_h + gap) - gap
    parts.append(f'<line class="axis" x1="{mid:.1f}" x2="{mid:.1f}" y1="{top - 4}" y2="{bottom + 4}"/>')
    return _svg(bottom + 10, title, desc, "".join(parts))


# --------------------------------------------------------------------------- #
# Action grid: recommended actions by effort and impact
# --------------------------------------------------------------------------- #
@dataclass
class ActionMark:
    n: int
    effort: int
    impact: int
    tip: str


def action_grid(marks: Sequence[ActionMark], *, title: str, desc: str) -> Markup:
    spots = {(max(1, min(5, m.effort)), max(1, min(5, m.impact))) for m in marks}
    if len(marks) < 2 or len(spots) < 2:  # all in one spot says nothing
        return Markup("")
    cell, left, top = 64, 64, 8
    parts = []
    for imp in range(1, 6):
        for eff in range(1, 6):
            x, y = left + (eff - 1) * cell, top + (5 - imp) * cell
            level = (2 if eff <= 2 else 1) if imp >= 4 else 0  # quick wins darkest, then big bets
            parts.append(f'<rect class="cell l{level}" x="{x + 1}" y="{y + 1}" width="{cell - 2}" height="{cell - 2}" rx="2"/>')
        parts.append(f'<text class="tick" x="{left - 10}" y="{top + (5 - imp) * cell + cell / 2}" text-anchor="end" '
                     f'dominant-baseline="central">{imp}</text>')
    for eff in range(1, 6):
        parts.append(f'<text class="tick" x="{left + (eff - 1) * cell + cell / 2}" y="{top + 5 * cell + 16}" '
                     f'text-anchor="middle">{eff}</text>')
    for label, col, row in (("Quick wins", 0, 0), ("Big bets", 2, 0), ("Fill-ins", 0, 2), ("Plan carefully", 2, 2)):
        parts.append(f'<text class="zone" x="{left + col * cell + 6}" y="{top + row * cell + 14}">{label}</text>')
    parts.append(f'<line class="axis" x1="{left + 2 * cell}" x2="{left + 2 * cell}" y1="{top}" y2="{top + 5 * cell}"/>'
                 f'<line class="axis" x1="{left}" x2="{left + 5 * cell}" y1="{top + 2 * cell}" y2="{top + 2 * cell}"/>'
                 f'<text class="axis-name" x="{left + 2.5 * cell}" y="{top + 5 * cell + 36}" text-anchor="middle">Effort</text>'
                 f'<text class="axis-name" transform="translate({left - 40},{top + 2.5 * cell}) rotate(-90)" text-anchor="middle">Impact</text>')
    slots: dict[tuple[int, int], int] = {}
    for m in marks:
        key = (max(1, min(5, m.effort)), max(1, min(5, m.impact)))
        k = slots.get(key, 0)
        slots[key] = k + 1
        cx = left + (key[0] - 1) * cell + 14 + (k % 3) * 18
        cy = top + (5 - key[1]) * cell + 26 + (k // 3) * 18
        parts.append(f'<g><title>{_e(m.tip)}</title><circle class="pin" cx="{cx}" cy="{cy}" r="8.5"/>'
                     f'<text class="pin-n" x="{cx}" y="{cy}" text-anchor="middle" dominant-baseline="central">{m.n}</text></g>')
    return _svg(top + 5 * cell + 46, title, desc, "".join(parts), cls="narrow", width=left + 5 * cell + 16)


__all__ = ["ActionMark", "Bar", "Diverge", "Event", "Point", "RiskMark", "Series", "Stack", "action_grid", "diverging",
           "hbar", "lines", "money", "nice_max", "risk_matrix", "scatter", "stacked", "timeline"]
