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


def render_svg(points, fov_arcsec=None, size=420, title=None):
    """Inline SVG scatter of the uncertainty cloud.

    Scaled to the cloud, not to the field: for a well-constrained object the
    field is a hundred times wider, and drawing to that scale would leave a
    single dot in an empty square. The field outline is drawn only when it is
    comparable in size; otherwise the caller reports coverage as text.
    """
    if not points:
        return None

    fov = fov_arcsec or config.FOV_ARCSEC
    ex, ey = extent(points)
    cloud_half = max(ex, ey, 1.0) * 1.18
    fov_half = fov / 2.0

    # Show the field only when it would actually be visible next to the cloud.
    show_fov = fov_half <= cloud_half * 1.6
    view_half = max(cloud_half, fov_half * 1.12) if show_fov else cloud_half

    pad = 34
    inner = size - pad * 2
    scale = inner / (2 * view_half)

    def px(v):   # RA offset increases to the left, as on a sky chart
        return pad + inner / 2 - v * scale

    def py(v):
        return pad + inner / 2 - v * scale

    parts = [
        f'<svg viewBox="0 0 {size} {size}" width="100%" '
        f'style="max-width:{size}px;display:block" role="img" '
        f'aria-label="Sky-plane uncertainty, {len(points)} variant orbits">',
        f'<rect x="{pad}" y="{pad}" width="{inner}" height="{inner}" '
        f'fill="#0a0c14" stroke="#242b3a"/>',
    ]

    for t in _ticks(view_half):
        x, y = px(t), py(t)
        if pad < x < pad + inner:
            parts.append(f'<line x1="{x:.1f}" y1="{pad}" x2="{x:.1f}" '
                         f'y2="{pad + inner}" stroke="#171d28"/>')
            parts.append(f'<text x="{x:.1f}" y="{pad + inner + 13}" '
                         f'fill="#556074" font-size="9" text-anchor="middle" '
                         f'font-family="monospace">{t:g}</text>')
        if pad < y < pad + inner:
            parts.append(f'<line x1="{pad}" y1="{y:.1f}" x2="{pad + inner}" '
                         f'y2="{y:.1f}" stroke="#171d28"/>')
            parts.append(f'<text x="{pad - 5}" y="{y + 3:.1f}" fill="#556074" '
                         f'font-size="9" text-anchor="end" '
                         f'font-family="monospace">{t:g}</text>')

    cx, cy = px(0), py(0)
    parts.append(f'<line x1="{cx:.1f}" y1="{pad}" x2="{cx:.1f}" '
                 f'y2="{pad + inner}" stroke="#2a3a52"/>')
    parts.append(f'<line x1="{pad}" y1="{cy:.1f}" x2="{pad + inner}" '
                 f'y2="{cy:.1f}" stroke="#2a3a52"/>')

    if show_fov:
        w = fov * scale
        parts.append(
            f'<rect x="{cx - w / 2:.1f}" y="{cy - w / 2:.1f}" width="{w:.1f}" '
            f'height="{w:.1f}" fill="rgba(95,201,212,.05)" stroke="#5fc9d4" '
            f'stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{cx - w / 2 + 4:.1f}" y="{cy - w / 2 - 5:.1f}" '
                     f'fill="#5fc9d4" font-size="9.5" '
                     f'font-family="monospace">field {fov:g}&#8243;</text>')

    parts.append(f'<g fill="#f0a63c" fill-opacity="0.45">')
    for x, y in points:
        parts.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="1.5"/>')
    parts.append("</g>")

    # Nominal solution: zero offset by definition.
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.2" fill="none" '
                 f'stroke="#dfe4ee" stroke-width="1.4"/>')

    parts.append(f'<text x="{size / 2:.0f}" y="{size - 6}" fill="#556074" '
                 f'font-size="9.5" text-anchor="middle" font-family="monospace">'
                 f'R.A. offset &#8243; &rarr; east left</text>')
    parts.append(f'<text x="11" y="{size / 2:.0f}" fill="#556074" font-size="9.5" '
                 f'text-anchor="middle" font-family="monospace" '
                 f'transform="rotate(-90 11 {size / 2:.0f})">Decl. offset &#8243;</text>')
    parts.append("</svg>")
    return "".join(parts)
