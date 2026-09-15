"""Small time-series line charts for one NEOCP object's polled history --
how its digest2 score, V magnitude, or any other column changed while it
sat on the confirmation page. Data comes from neocp_history.py's database;
this module only draws it.

Kept deliberately simple relative to moonplot.py/uncertainty.py: no fixed
axes, no zoom, no gap detection -- polls are roughly evenly spaced already,
and this is a read-only history view rather than a live observing tool.
"""

import re
import time


def _nice_step(span, max_ticks, min_step=0.0):
    for step in (0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500):
        if step >= min_step and span / step <= max_ticks:
            return step
    return step


def _ticks(lo, hi, max_ticks=5, min_step=0.0):
    if hi <= lo:
        return [lo]
    step = _nice_step(hi - lo, max_ticks, min_step)
    start = step * (int(lo // step))
    out, v = [], start
    while v <= hi + step * 1e-9:
        if v >= lo - step * 1e-9:
            out.append(round(v, 6))
        v += step
    return out


def line_svg(points, label, color="#5fc9d4", size_w=1100, size_h=220,
             fmt="{:.1f}", y_max=None):
    """points: [(unix_ts, value), ...], oldest first. None values are
    dropped rather than plotted, since a missing reading is not zero.

    size_w/size_h set the SVG's internal viewBox, not its displayed size --
    the element has no width cap (see the style attribute below), so it
    scales to whatever width its container actually gives it, height
    following automatically to keep this box's own proportions. Chosen wide
    to begin with so that scaling is usually modest rather than the 1000%+
    stretch a small fixed-size chart would need to fill a real page.

    y_max pins the top of the axis to a known ceiling (digest2 score can
    never exceed 100) instead of padding above the data's own max, which
    would otherwise make a run of near-100 scores look like it still has
    room to climb. The bottom is always padded off the data as usual --
    only the top is ever fixed.
    """
    pts = [(t, v) for t, v in points if v is not None]
    if len(pts) < 2:
        return None

    # Ticks must never be closer together than fmt can actually distinguish
    # (e.g. "{:.0f}" can't tell 99.8 from 100.0 apart) -- otherwise the axis
    # ends up printing the same rounded label several times over.
    decimals_match = re.search(r"\.(\d+)f", fmt)
    min_step = 10 ** -int(decimals_match.group(1)) if decimals_match else 1

    t0, t1 = pts[0][0], pts[-1][0]
    tspan = max(t1 - t0, 1.0)
    vals = [v for _, v in pts]
    vmin, vmax = min(vals), max(vals)
    pad_v = max((vmax - vmin) * 0.15, min_step)
    lo = vmin - pad_v
    hi = y_max if y_max is not None else vmax + pad_v

    pad_l, pad_r, pad_t, pad_b = 54, 18, 22, 34
    inner_w = size_w - pad_l - pad_r
    inner_h = size_h - pad_t - pad_b

    def px(t):
        return pad_l + (t - t0) / tspan * inner_w

    def py(v):
        return pad_t + inner_h - (v - lo) / (hi - lo) * inner_h

    parts = [
        f'<svg viewBox="0 0 {size_w} {size_h}" width="100%" '
        f'style="display:block;height:auto" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="{label} over time">',
        f'<rect x="{pad_l}" y="{pad_t}" width="{inner_w}" height="{inner_h}" '
        f'fill="#0a0c14" stroke="#242b3a"/>',
    ]

    for v in _ticks(lo, hi, min_step=min_step):
        yy = py(v)
        if pad_t <= yy <= pad_t + inner_h:
            parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l + inner_w}" '
                         f'y2="{yy:.1f}" stroke="#171d28"/>')
            parts.append(f'<text x="{pad_l - 6}" y="{yy + 4:.1f}" fill="#556074" '
                         f'font-size="11" text-anchor="end" '
                         f'font-family="monospace">{fmt.format(v)}</text>')

    # More time ticks than the old fixed-size version could fit -- a wide
    # canvas has the room, and a sparse axis would look empty rather than
    # simple.
    n_ticks = min(9, len(pts) - 1) or 1
    for i in range(n_ticks + 1):
        ts = t0 + tspan * i / n_ticks
        xx = px(ts)
        parts.append(f'<line x1="{xx:.1f}" y1="{pad_t}" x2="{xx:.1f}" '
                     f'y2="{pad_t + inner_h}" stroke="#171d28"/>')
        lbl = time.strftime("%m-%d %H:%M", time.gmtime(ts))
        parts.append(f'<text x="{xx:.1f}" y="{pad_t + inner_h + 16}" '
                     f'fill="#556074" font-size="9.5" text-anchor="middle" '
                     f'font-family="monospace">{lbl}</text>')

    path = " ".join(f'{"M" if i == 0 else "L"}{px(t):.1f},{py(v):.1f}'
                    for i, (t, v) in enumerate(pts))
    parts.append(f'<path d="{path}" fill="none" stroke="{color}" '
                 f'stroke-width="2"/>')
    for t, v in pts:
        parts.append(f'<circle cx="{px(t):.1f}" cy="{py(v):.1f}" r="2.6" '
                     f'fill="{color}"><title>'
                     f'{time.strftime("%Y-%m-%d %H:%M", time.gmtime(t))} UT '
                     f'&#8212; {label} {fmt.format(v)}</title></circle>')

    parts.append(f'<text x="{pad_l + 4}" y="{pad_t + 12}" fill="{color}" '
                 f'font-size="12" font-family="monospace">{label}</text>')
    parts.append("</svg>")
    return "".join(parts)
