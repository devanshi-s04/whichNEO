"""Twin altitude plot, in the classic staralt style.

staralt (the ING/La Palma visibility tool, and every clone descended from it)
does not plot Moon *distance* as its own line -- it plots two altitude curves
against the same time axis, one per body, and lets the reader see how close
together they run. That is reproduced here: the object's altitude (r.alt)
and the Moon's altitude (r.moon_alt) are both already present on every row of
the ephemeris MPC already sent us -- see ephemeris.py's Row -- so this is
free of any extra fetch or computation.

Drawn across dusk to dawn -- evening and morning twilight, from the Sun's
own altitude -- not the narrower window pipeline.py filtered down to
"usable" rows, and not however far MPC's altitude-gated ephemeris happens to
run past twilight either (MPC's own request asks for oalt=20, see
ephemeris._post, so the raw feed can include broad-daylight rows with the
object merely above 20 degrees). Re-filtering by sun/Moon/azimuth on top of
that would carve extra holes out of an otherwise evenly time-stepped series;
the observability window is instead drawn as a shaded band on top of the
continuous curves, the way staralt shades twilight on top of its curves
rather than cutting them.
"""

import time

import config


def _site(site):
    """The site to work on. None means the deployment's default."""
    return config.DEFAULT_SITE if site is None else site


def _y_ticks(lo, hi):
    for step in (10, 15, 30, 45, 60, 90):
        if (hi - lo) / step <= 6:
            break
    out, v = [], step * (int(lo // step))
    while v <= hi:
        if v >= lo:
            out.append(v)
        v += step
    return out


def _crossing_ts(a, b, thresh):
    """Where sun_alt crosses thresh between two rows, by linear interpolation."""
    if b.sun_alt == a.sun_alt:
        return None
    frac = (thresh - a.sun_alt) / (b.sun_alt - a.sun_alt)
    return a.ts + frac * (b.ts - a.ts) if 0.0 <= frac <= 1.0 else None


def _twilight_bounds(pts, site=None):
    """Evening twilight (dusk) and morning twilight (dawn), i.e. where the
    Sun crosses the observatory's own dark-enough-to-observe threshold
    (the site's sun_alt_max) -- found from sun_alt, which every cached row
    already carries, rather than a fresh astropy call on page load. Falls
    back to the data's own first/last timestamp when a crossing isn't
    bracketed by any two rows (an ephemeris that never reaches daylight).
    """
    thresh = _site(site).sun_alt_max
    dusk = dawn = None
    for a, b in zip(pts, pts[1:]):
        if a.sun_alt > thresh >= b.sun_alt and dusk is None:
            dusk = _crossing_ts(a, b, thresh)
        elif a.sun_alt <= thresh < b.sun_alt:
            c = _crossing_ts(a, b, thresh)
            if c is not None:
                dawn = c   # keep the last rising crossing, closest to dawn
    return (dusk if dusk is not None else pts[0].ts,
            dawn if dawn is not None else pts[-1].ts)


def render_svg(rows, window_start_ts=None, window_end_ts=None,
                size_w=560, size_h=320, localt=None, tzlabel=None,
                moon_track=None, site=None):
    """Inline SVG: object altitude and Moon altitude vs. time, with the
    angular separation to the Moon printed under every point.

    rows: ephemeris.Row objects (or anything with .ts, .alt, .moon_alt,
    .sun_alt), drawn across dusk-to-dawn -- evening and morning twilight, by
    the Sun's own altitude -- rather than however far MPC's altitude-gated
    ephemeris happens to run either side of the night. window_start_ts/
    window_end_ts, when given, are shaded as the sub-span pipeline.py
    determined to actually be usable, but never clip the curves themselves.

    rows can (and, from app.py, does) already be a merge of the normal
    oalt=20 fetch with a second, wider one scoped only to this plot -- see
    ephemeris.fetch_gap_fill -- so a hole in the ordinary feed doesn't
    necessarily mean a hole here too.

    moon_track, when given, is [(unix_ts, moon_alt_deg), ...] computed
    independently (see observability.moon_state) rather than read off rows.
    The Moon's position needs no orbit fit -- unlike the object's, it is
    exactly knowable for any instant -- so when this covers the plotted
    span, the Moon curve is drawn gap-free even where the object's own
    curve still has a real hole. Falls back to each row's own moon_alt
    (and that hole) when not supplied or too sparse.

    localt formats a timestamp for the axis. The board shows observatory local
    time everywhere, because the people reading it are standing in the dome;
    UT stays in the hover text, since that is what MPC and the plan file use.
    Without it the axis falls back to UT.

    size_w is a floor, not a fixed width: one label per point needs its own
    horizontal slot, so the plot widens automatically once there are enough
    points that a fixed width would run them into each other.
    """
    s = _site(site)
    fmt = localt or (lambda ts: time.strftime("%H:%M", time.gmtime(ts)))
    pts = sorted(rows, key=lambda r: r.ts) if rows else []
    if len(pts) < 2:
        return None

    dusk_ts, dawn_ts = _twilight_bounds(pts, s)
    in_night = [r for r in pts if dusk_ts <= r.ts <= dawn_ts]
    if len(in_night) >= 2:
        pts = in_night
        t0, t1 = dusk_ts, dawn_ts
    else:
        t0, t1 = pts[0].ts, pts[-1].ts
    tspan = max(t1 - t0, 1.0)

    # Independent Moon track for this same span, when the caller supplied
    # enough of one; otherwise fall back to reading moon_alt off the
    # object's own rows, gaps and all, exactly as before.
    moon_pts = sorted((ts, alt) for ts, alt in (moon_track or [])
                      if t0 <= ts <= t1)
    moon_from_rows = len(moon_pts) < 2
    if moon_from_rows:
        moon_pts = [(r.ts, r.moon_alt) for r in pts]

    vals = [r.alt for r in pts] + [v for _, v in moon_pts]
    vmin, vmax = min(vals), max(vals)
    lo = min(0.0, vmin - 5.0)
    hi = max(90.0, vmax + 5.0) if vmax > 80 else vmax + 8.0
    hi = max(hi, lo + 20.0)

    # Bottom strip holds, top to bottom: the UT tick row (existing), the
    # rotated per-point separation labels (new -- ~3 chars at this font size
    # need about 16px of vertical run), a caption naming what those numbers
    # are, then the time-axis caption.
    pad_l, pad_r, pad_t = 34, 12, 16
    label_band_h = 20
    pad_b = 13 + 6 + label_band_h + 14 + 14
    size_h = max(size_h, pad_t + 20 + pad_b)

    # Each point needs its own horizontal slot for the rotated label below
    # it, or neighbours overlap -- widen rather than crowd them.
    min_px_per_point = 13
    inner_w = max(size_w - pad_l - pad_r, (len(pts) - 1) * min_px_per_point)
    size_w = pad_l + inner_w + pad_r
    inner_h = size_h - pad_t - pad_b

    def px(ts):
        return pad_l + (ts - t0) / tspan * inner_w

    def py(deg):
        return pad_t + inner_h - (deg - lo) / (hi - lo) * inner_h

    # MPC's own ephemeris can have a real hole in it -- an object dipping
    # briefly under their server-side altitude floor drops rows entirely for
    # that stretch, even though the steps on either side are perfectly even.
    # Detected once here so both the background marker and the curves below
    # agree on where the actual gaps are.
    step_deltas = [b.ts - a.ts for a, b in zip(pts, pts[1:])]
    typical_dt = min(step_deltas) if step_deltas else 0
    gap_after = ({i for i, d in enumerate(step_deltas) if d > typical_dt * 1.8}
                 if typical_dt else set())

    parts = [
        f'<svg viewBox="0 0 {size_w} {size_h}" width="100%" '
        f'style="max-width:{size_w}px;display:block" role="img" '
        f'aria-label="Object and Moon altitude across the night">',
        f'<rect x="{pad_l}" y="{pad_t}" width="{inner_w}" height="{inner_h}" '
        f'fill="#0a0c14" stroke="#242b3a"/>',
    ]

    # Mark real holes in MPC's data (see gap_after above) so blank canvas
    # reads as "nothing was returned here", not as a rendering glitch.
    for i in gap_after:
        x0, x1 = px(pts[i].ts), px(pts[i + 1].ts)
        parts.append(f'<rect x="{x0:.1f}" y="{pad_t}" width="{x1 - x0:.1f}" '
                     f'height="{inner_h}" fill="#12151f"/>')
        parts.append(f'<text x="{(x0 + x1) / 2:.1f}" y="{pad_t + inner_h / 2:.1f}" '
                     f'fill="#3c4456" font-size="8.5" text-anchor="middle" '
                     f'font-family="monospace">no data</text>')

    # The usable sub-window, shaded behind the curves rather than clipping
    # them -- the same idea as staralt shading twilight on top of its plot.
    if window_start_ts is not None and window_end_ts is not None:
        ws, we = max(window_start_ts, t0), min(window_end_ts, t1)
        if we > ws:
            xw0, xw1 = px(ws), px(we)
            parts.append(f'<rect x="{xw0:.1f}" y="{pad_t}" '
                         f'width="{xw1 - xw0:.1f}" height="{inner_h}" '
                         f'fill="rgba(95,201,212,.07)"/>')
            parts.append(f'<text x="{(xw0 + xw1) / 2:.1f}" y="{pad_t + inner_h - 6}" '
                         f'fill="#5fc9d4" font-size="8.5" text-anchor="middle" '
                         f'font-family="monospace">observable window</text>')

    for d in _y_ticks(lo, hi):
        yy = py(d)
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l + inner_w}" '
                     f'y2="{yy:.1f}" stroke="#171d28"/>')
        parts.append(f'<text x="{pad_l - 5}" y="{yy + 3:.1f}" fill="#556074" '
                     f'font-size="9" text-anchor="end" '
                     f'font-family="monospace">{d:g}&#176;</text>')

    n_ticks = min(4, len(pts) - 1) or 1
    for i in range(n_ticks + 1):
        ts = t0 + tspan * i / n_ticks
        xx = px(ts)
        parts.append(f'<line x1="{xx:.1f}" y1="{pad_t}" x2="{xx:.1f}" '
                     f'y2="{pad_t + inner_h}" stroke="#171d28"/>')
        label = fmt(ts)
        parts.append(f'<text x="{xx:.1f}" y="{pad_t + inner_h + 13}" '
                     f'fill="#556074" font-size="9" text-anchor="middle" '
                     f'font-family="monospace">{label}</text>')

    # The dashed cutoff line, in the staralt convention. It draws MPC's own
    # floor rather than the site's min_alt: we pass oalt=20 in
    # ephemeris._post, so MPC never returns a row below 20 degrees and the
    # curve physically cannot go under this line. min_alt is 15 and therefore
    # dead -- drawing it would imply a dome limit that never actually applies,
    # and leave empty space beneath the curve that no data could ever occupy.
    # Which of the two the observatory really wants is open question 4 in
    # TBD.md.
    floor = s.mpc_server_min_alt
    if lo <= floor <= hi:
        yy = py(floor)
        parts.append(f'<line x1="{pad_l}" y1="{yy:.1f}" x2="{pad_l + inner_w}" '
                     f'y2="{yy:.1f}" stroke="#c75c5c" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{pad_l + inner_w - 4}" y="{yy - 4:.1f}" '
                     f'fill="#c75c5c" font-size="9" text-anchor="end" '
                     f'font-family="monospace">MPC cutoff {floor:g}&#176;'
                     f'</text>')

    # A straight line across one of the gaps found above would claim data
    # that was never returned; break the path there instead.
    def curve(attr, color, dash=None):
        segs = []
        for i, r in enumerate(pts):
            cmd = "M" if (i == 0 or (i - 1) in gap_after) else "L"
            segs.append(f'{cmd}{px(r.ts):.1f},{py(getattr(r, attr)):.1f}')
        dasharray = f' stroke-dasharray="{dash}"' if dash else ""
        return (f'<path d="{" ".join(segs)}" fill="none" stroke="{color}" '
                f'stroke-width="1.6"{dasharray}/>')

    parts.append(curve("alt", "#5fc9d4"))

    if moon_from_rows:
        parts.append(curve("moon_alt", "#9aa3b8", dash="5 3"))
    else:
        # An independent track has no MPC-shaped holes to break at -- the
        # Moon's position is exactly knowable for every instant in range.
        moon_path = " ".join(
            f'{"M" if i == 0 else "L"}{px(t):.1f},{py(v):.1f}'
            for i, (t, v) in enumerate(moon_pts))
        parts.append(f'<path d="{moon_path}" fill="none" stroke="#9aa3b8" '
                     f'stroke-width="1.6" stroke-dasharray="5 3"/>')

    # Point colour flags the Moon-separation check itself: green when the
    # object clears the observatory's minimum separation at that timestamp,
    # orange when it does not -- independent of whether the row also failed
    # some other criterion (altitude, sun, azimuth) that kept it out of the
    # shaded usable window above.
    label_y0 = pad_t + inner_h + 13 + 6   # below the tick-label row
    for r in pts:
        clear = r.moon_dist > s.moon_sep_min
        color = "var(--go)" if clear else "var(--warn)"
        cx = px(r.ts)
        # Local on the axis, both here: UT is what MPC, the plan file and the
        # 80-column astrometry all speak, so it has to stay reachable.
        stamp = f'{fmt(r.ts)} ({time.strftime("%H:%M", time.gmtime(r.ts))} UT)'
        parts.append(f'<circle cx="{cx:.1f}" cy="{py(r.alt):.1f}" r="2.2" '
                     f'fill="{color}" stroke="#0a0c14" stroke-width="0.6">'
                     f'<title>{stamp}'
                     f' &#8212; object {r.alt:.0f}&#176;, Moon {r.moon_alt:.0f}&#176;, '
                     f'{r.moon_dist:.0f}&#176; apart '
                     f'({"clears" if clear else "within"} the '
                     f'{s.moon_sep_min:g}&#176; limit)</title></circle>')
        # Angular separation, written vertically under its point -- rotating
        # turns the label's reading direction downward instead of sideways,
        # so points spaced only ~11px apart still get a legible label each.
        parts.append(f'<text x="{cx:.1f}" y="{label_y0}" fill="{color}" '
                     f'font-size="7.2" font-family="monospace" '
                     f'transform="rotate(90 {cx:.1f} {label_y0})">'
                     f'{r.moon_dist:.0f}&#176;</text>')

    # Legend, top-left inside the plot -- line style doubles for colour-blind
    # readers, since object is solid and Moon is dashed. Backed by a solid
    # panel: the object curve's peak often sits right behind it otherwise.
    # Placed in whichever top corner the curves come nearest to missing, rather
    # than pinned left behind an opaque panel. Masking the overlap hides real
    # data: a target setting through the night starts high on the left, so a
    # fixed left-hand legend swallowed its first couple of hours entirely.
    # Rising and setting targets want opposite corners, so measure instead of
    # guessing.
    legend_w, legend_h = 132, 56

    def _clearance(ox, oy):
        """Smallest distance from a legend box at (ox, oy) to any plotted point,
        on either curve -- checked against the Moon track actually drawn,
        not just wherever the object happens to have a row."""
        x0, x1 = ox - 6, ox - 6 + legend_w
        y0, y1 = oy - 10, oy - 10 + legend_h
        worst = float("inf")
        points = [(r.ts, r.alt) for r in pts] + moon_pts
        for t, value in points:
            x, y = px(t), py(value)
            dx = max(x0 - x, 0.0, x - x1)
            dy = max(y0 - y, 0.0, y - y1)
            worst = min(worst, (dx * dx + dy * dy) ** 0.5)
        return worst

    lx, ly = max([(pad_l + 8, pad_t + 12),
                  (pad_l + inner_w - legend_w - 2, pad_t + 12)],
                 key=lambda c: _clearance(*c))
    parts.append(f'<rect x="{lx - 6}" y="{ly - 10}" width="{legend_w}" '
                 f'height="{legend_h}" fill="#0a0c14" fill-opacity="0.88" rx="3"/>')
    parts.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 16}" y2="{ly}" '
                 f'stroke="#5fc9d4" stroke-width="1.6"/>')
    parts.append(f'<text x="{lx + 20}" y="{ly + 3}" fill="#9fb0c3" '
                 f'font-size="9" font-family="monospace">object</text>')
    parts.append(f'<line x1="{lx}" y1="{ly + 13}" x2="{lx + 16}" y2="{ly + 13}" '
                 f'stroke="#9aa3b8" stroke-width="1.6" stroke-dasharray="5 3"/>')
    parts.append(f'<text x="{lx + 20}" y="{ly + 16}" fill="#9fb0c3" '
                 f'font-size="9" font-family="monospace">Moon</text>')
    parts.append(f'<circle cx="{lx + 8}" cy="{ly + 26}" r="2.2" fill="var(--go)"/>')
    parts.append(f'<text x="{lx + 20}" y="{ly + 29}" fill="#9fb0c3" font-size="9" '
                 f'font-family="monospace">&gt;{s.moon_sep_min:g}&#176; from Moon</text>')
    parts.append(f'<circle cx="{lx + 8}" cy="{ly + 39}" r="2.2" fill="var(--warn)"/>')
    parts.append(f'<text x="{lx + 20}" y="{ly + 42}" fill="#9fb0c3" font-size="9" '
                 f'font-family="monospace">&#8804;{s.moon_sep_min:g}&#176; '
                 f'from Moon</text>')

    parts.append(f'<text x="{size_w / 2:.0f}" y="{size_h - 4}" fill="#556074" '
                 f'font-size="9.5" text-anchor="middle" font-family="monospace">'
                 f'time ({tzlabel or "UT"})</text>')
    parts.append(f'<text x="11" y="{size_h / 2:.0f}" fill="#556074" font-size="9.5" '
                 f'text-anchor="middle" font-family="monospace" '
                 f'transform="rotate(-90 11 {size_h / 2:.0f})">Altitude</text>')
    # Names the per-point numbers above -- a horizontal caption rather than a
    # rotated axis title, since that title's own text is far longer than the
    # ~20px band the per-point labels need, and would overrun the canvas
    # edge if forced to rotate in place alongside them.
    parts.append(f'<text x="{size_w / 2:.0f}" y="{label_y0 + label_band_h + 10:.0f}" '
                 f'fill="#556074" font-size="8.5" text-anchor="middle" '
                 f'font-family="monospace">Moon separation (&#176;), per point</text>')

    parts.append("</svg>")
    return "".join(parts)
