"""Sky-plane uncertainty plot, drawn from MPC's own variant-orbit offsets.

MPC's uncertainty map is a scatter of ~2000 variant orbits, each a predicted
position offset in arcseconds from the nominal solution. Those numbers are on
the Offsets page, which we already fetch to compute scatteredness -- we simply
kept the extremes and threw the points away.

Drawing them ourselves rather than embedding MPC's picture avoids hotlinking a
temp file whose name expires, costs no request when a page loads, and lets us
overlay the telescope field, which is the question an observer actually has:
can I catch this in one pointing? That overlay is what the legacy MPCS tool
existed to provide.
"""

import config


def coverage(points, fov_arcsec=None):
    """Fraction of the uncertainty cloud inside one centred pointing.

    The field is square and centred on the nominal position, matching how the
    legacy tool treated it.
    """
    if not points:
        return None
    half = (fov_arcsec or config.FOV_ARCSEC) / 2.0
    inside = sum(1 for x, y in points if abs(x) <= half and abs(y) <= half)
    return inside / len(points)


def extent(points):
    """Half-width of the cloud in arcseconds, as (dx, dy)."""
    if not points:
        return (0.0, 0.0)
    return (max(abs(x) for x, _ in points), max(abs(y) for _, y in points))


def distinct(points):
    """How many separate grid positions the variants occupy.

    MPC serves offsets rounded to whole arcseconds. When a cloud is only a
    few arcseconds across, thousands of variants land on a few positions, and
    this number says how coarse the sampling has become.
    """
    return len(set(points)) if points else 0


def _ticks(half):
    """A few round gridline values inside +/- half."""
    for step in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000):
        if half / step <= 4:
            break
    out, v = [], step
    while v < half:
        out.extend((-v, v))
        v += step
    return sorted(out)


# 1300" either side of nominal -- config.FOV_ARCSEC's own half-width, so the
# fixed frame is exactly one field of view: the box drawn from fov_half below
# now touches the frame's edges exactly, rather than sitting at half its size.
AXIS_HALF_ARCSEC = config.FOV_ARCSEC / 2.0


def render_svg(points, fov_arcsec=None, size=420, title=None):
    """Inline SVG scatter of the uncertainty cloud.

    Axes are pinned to +/-AXIS_HALF_ARCSEC by default, rather than scaled to
    the cloud, so a given offset sits at the same pixel position on every
    target's plot and the panels are directly comparable at a glance.

    svg_zoom.js rescales this interactively: the frame, gridlines and tick
    labels stay fixed on screen while their *values* change with the zoom
    level (a real axis, not a picture being stretched). To do that it needs
    to redraw the axis itself, so the SVG is split into an axis layer (server
    -rendered here for the no-JS case, then wholesale replaced on every zoom/
    pan) and a content layer (dots, nominal ring, field box) that JS instead
    moves with a transform, clipped to the fixed frame regardless of that
    transform. data-pad/data-inner/data-scale on the root element are the
    only pieces of this mapping JS needs to reconstruct it.
    """
    if not points:
        return None

    fov = fov_arcsec or config.FOV_ARCSEC
    fov_half = fov / 2.0
    view_half = AXIS_HALF_ARCSEC

    # Show the field only when it would actually be visible at this scale.
    show_fov = fov_half <= view_half * 1.6

    pad = 34
    inner = size - pad * 2
    scale = inner / (2 * view_half)

    def px(v):   # RA offset increases to the left, as on a sky chart
        return pad + inner / 2 - v * scale

    def py(v):
        return pad + inner / 2 - v * scale

    parts = [
        f'<svg viewBox="0 0 {size} {size}" width="100%" class="zoomable" '
        f'style="max-width:{size}px;display:block;cursor:grab;touch-action:none" '
        f'role="img" '
        f'aria-label="Sky-plane uncertainty, {len(points)} variant orbits" '
        f'data-pad="{pad}" data-inner="{inner}" data-scale="{scale}">',
        f'<rect x="{pad}" y="{pad}" width="{inner}" height="{inner}" '
        f'fill="#0a0c14" stroke="#242b3a"/>',
        # Content (below) pans/zooms; this clip is on a non-transformed
        # ancestor of it, so the visible frame never moves regardless.
        f'<clipPath id="uncClip"><rect x="{pad}" y="{pad}" width="{inner}" '
        f'height="{inner}"/></clipPath>',
    ]

    parts.append(f'<g id="uncAxis">{_axis_svg(pad, inner, view_half, px, py)}</g>')

    cx, cy = px(0), py(0)

    # Two sibling groups share the "uncContent" class -- svg_zoom.js sets the
    # same pan/zoom transform on both -- but only the dots are clipped. The
    # field label is deliberately placed just outside the box's top edge, and
    # clipping it along with the box would cut it off; wrapping *everything*
    # in one clipped group was the bug an earlier version of this had.
    parts.append('<g class="uncContent">')
    if show_fov:
        w = fov * scale
        parts.append(
            f'<rect x="{cx - w / 2:.1f}" y="{cy - w / 2:.1f}" width="{w:.1f}" '
            f'height="{w:.1f}" fill="rgba(95,201,212,.05)" stroke="#5fc9d4" '
            f'stroke-dasharray="4 3" vector-effect="non-scaling-stroke"/>')
        parts.append(f'<text x="{cx - w / 2 + 4:.1f}" y="{cy - w / 2 - 5:.1f}" '
                     f'fill="#5fc9d4" font-size="9.5" '
                     f'font-family="monospace">field {fov:g}&#8243;</text>')

    # MPC's Offsets page rounds to whole arcseconds, so for a tightly
    # constrained object thousands of variants collapse onto a handful of grid
    # positions -- P22pOYa's 2000 orbits land on 17. Drawing a dot per variant
    # would stack them invisibly and look far sparser than MPC's picture,
    # which is plotted from their unrounded values. Drawing one dot per
    # position, sized by how many variants share it, puts that density back.
    counts = {}
    for p in points:
        counts[p] = counts.get(p, 0) + 1
    peak = max(counts.values())

    # Nominal solution: zero offset by definition. Drawn here (unclipped,
    # inside the non-clipped content group) so it's never cut off even at a
    # zoom/pan where it would sit right at the frame's edge.
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.2" fill="none" '
                 f'stroke="#dfe4ee" stroke-width="1.4" '
                 f'vector-effect="non-scaling-stroke"/>')
    parts.append("</g>")   # .uncContent (unclipped half)

    parts.append('<g clip-path="url(#uncClip)"><g class="uncContent">')
    parts.append('<g fill="#f0a63c">')
    for (x, y), n in counts.items():
        frac = (n / peak) ** 0.5          # area, not radius, tracks the count
        r = 1.3 + 4.2 * frac
        op = 0.32 + 0.5 * frac
        parts.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" '
                     f'r="{r:.2f}" fill-opacity="{op:.2f}" '
                     f'vector-effect="non-scaling-stroke"><title>'
                     f'{x:+d}&#8243;, {y:+d}&#8243; &mdash; {n} of {len(points)} '
                     f'variants</title></circle>')
    parts.append("</g>")

    parts.append("</g>")   # .uncContent (clipped half)
    parts.append("</g>")   # clip wrapper

    parts.append(f'<text x="{size / 2:.0f}" y="{size - 6}" fill="#556074" '
                 f'font-size="9.5" text-anchor="middle" font-family="monospace">'
                 f'R.A. offset &#8243; &rarr; east left</text>')
    parts.append(f'<text x="11" y="{size / 2:.0f}" fill="#556074" font-size="9.5" '
                 f'text-anchor="middle" font-family="monospace" '
                 f'transform="rotate(-90 11 {size / 2:.0f})">Decl. offset &#8243;</text>')
    parts.append("</svg>")
    return "".join(parts)


def _axis_svg(pad, inner, half, px, py):
    """Gridlines + tick labels for the visible range +/-half. Split out so
    the server-rendered initial view and svg_zoom.js's redraws (see
    static/svg_zoom.js's buildAxis, a JS port of this same tick-picking
    logic) produce identical markup."""
    out = []
    for t in _ticks(half):
        x, y = px(t), py(t)
        if pad < x < pad + inner:
            out.append(f'<line x1="{x:.1f}" y1="{pad}" x2="{x:.1f}" '
                       f'y2="{pad + inner}" stroke="#171d28"/>')
            out.append(f'<text x="{x:.1f}" y="{pad + inner + 13}" '
                       f'fill="#556074" font-size="9" text-anchor="middle" '
                       f'font-family="monospace">{t:g}</text>')
        if pad < y < pad + inner:
            out.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{pad + inner}" '
                       f'y2="{y:.1f}" stroke="#171d28"/>')
            out.append(f'<text x="{pad - 5}" y="{y + 3:.1f}" fill="#556074" '
                       f'font-size="9" text-anchor="end" '
                       f'font-family="monospace">{t:g}</text>')

    cx, cy = px(0), py(0)
    if pad <= cx <= pad + inner:
        out.append(f'<line x1="{cx:.1f}" y1="{pad}" x2="{cx:.1f}" '
                   f'y2="{pad + inner}" stroke="#2a3a52"/>')
    if pad <= cy <= pad + inner:
        out.append(f'<line x1="{pad}" y1="{cy:.1f}" x2="{pad + inner}" '
                   f'y2="{cy:.1f}" stroke="#2a3a52"/>')
    return "".join(out)
