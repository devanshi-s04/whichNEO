"""All-sky map of tonight's targets, drawn as inline SVG.

Zenith at the centre, horizon at the rim, radius linear in altitude. Almost
every target we care about sits between 20 and 60 degrees, and a linear radius
gives that band the most room; a projection that crowds the horizon would
squeeze the part of the sky actually in use.

Orientation is the MAP convention, set deliberately in config: north up, east
RIGHT, south down, west left, so the disc reads like looking down on the
observatory the way dome azimuth is thought about. This is the mirror of a
planisphere, which puts east on the left because you hold it up against the
sky. The check that it is right is geographic: Trieste lies at bearing 3.0 deg
true from L01, so it must land essentially at the top of the disc.

Nothing here rejects anything. The horizon mask is drawn as tinted sky, and
targets inside it are plotted exactly like any other -- see the HARDNESS note
on the site's horizon mask for why that matters.
"""

import math
from html import escape

import config
import neodistance
import observability

_SECTOR_WIDTH = 45.0


def _site(site):
    """The site to work on. None means the deployment's default."""
    return config.DEFAULT_SITE if site is None else site


# --- geometry ---------------------------------------------------------------

def project(az_deg, alt_deg, cx, cy, radius):
    """Horizon coordinates to screen coordinates.

    North up, east right, south down, west left. Altitude 90 lands on the
    centre, altitude 0 on the rim.
    """
    r = radius * (90.0 - alt_deg) / 90.0
    a = math.radians(az_deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def _radius_for(alt_deg, radius):
    return radius * (90.0 - alt_deg) / 90.0


def _polar(cx, cy, r, az_deg):
    a = math.radians(az_deg)
    return cx + r * math.sin(a), cy - r * math.cos(a)


def _wedge_path(cx, cy, r_out, r_in, az0, az1):
    """An annular wedge between two radii, spanning az0 to az1.

    Azimuth increases clockwise on screen, which is SVG's positive sweep
    direction, so the outer arc uses sweep 1 and the inner one comes back
    with sweep 0.
    """
    x0, y0 = _polar(cx, cy, r_out, az0)
    x1, y1 = _polar(cx, cy, r_out, az1)
    large = 1 if (az1 - az0) % 360.0 > 180.0 else 0
    if r_in <= 0.5:
        return (f"M{x0:.1f} {y0:.1f} A{r_out:.1f} {r_out:.1f} 0 {large} 1 "
                f"{x1:.1f} {y1:.1f} L{cx:.1f} {cy:.1f} Z")
    x2, y2 = _polar(cx, cy, r_in, az1)
    x3, y3 = _polar(cx, cy, r_in, az0)
    return (f"M{x0:.1f} {y0:.1f} A{r_out:.1f} {r_out:.1f} 0 {large} 1 "
            f"{x1:.1f} {y1:.1f} L{x2:.1f} {y2:.1f} "
            f"A{r_in:.1f} {r_in:.1f} 0 {large} 0 {x3:.1f} {y3:.1f} Z")


# --- turning the queue into markers ----------------------------------------

def _interpolate(track, ts):
    """Alt/az at `ts`, between the bracketing ephemeris samples.

    Outside the track it clamps to the nearer END. Falling back to track[0]
    regardless -- which this did originally -- puts a target that has just run
    off the end of its ephemeris back at the position it held when the
    ephemeris began, which can be a day earlier and most of the sky away. It
    showed up as A11GP9t reading azimuth 112 while it was actually near 76.
    """
    if ts <= track[0][0]:
        return track[0][1], track[0][2]
    if ts >= track[-1][0]:
        return track[-1][1], track[-1][2]
    for a, b in zip(track, track[1:]):
        if a[0] <= ts <= b[0]:
            span = b[0] - a[0]
            f = 0.0 if span == 0 else (ts - a[0]) / span
            d_az = ((b[1] - a[1] + 180.0) % 360.0) - 180.0
            return (a[1] + d_az * f) % 360.0, a[2] + (b[2] - a[2]) * f
    return track[-1][1], track[-1][2]


# MPC's uncertainty-map palette, so a marker here means what the same colour
# means on their map. Tuned for luminance on this background rather than
# copied as literal RGB -- these have to read at a glance on a dome screen.
DISTANCE_COLOURS = {
    neodistance.NEAR: "#e04b3c",       # red,       < 0.01 AU
    neodistance.CLOSE: "#f0862c",      # orange,    0.01 - 0.05 AU
    neodistance.MAIN_BELT: "#4a7fd8",  # dark blue, main-belt
    neodistance.FAR: "#3fae5a",        # green,     everything else
    neodistance.UNKNOWN: "#8d97a8",    # no distance could be derived
}

# Done is deliberately off MPC's list. Green used to mean "this observer has
# shot it", and now means "more than 0.05 AU away" -- the same colour cannot
# carry both. White belongs to no distance class, including the magenta MPC
# has reserved for Jupiter Trojans and not yet implemented.
DONE_COLOUR = "#ffffff"


def _mark_colour(mark):
    """What colour this marker is drawn in, and why, in one place."""
    if mark.get("observed"):
        return DONE_COLOUR
    return DISTANCE_COLOURS.get(mark.get("distance"),
                                DISTANCE_COLOURS[neodistance.UNKNOWN])


def _distance_phrase(mark):
    """The distance in words, for the marker's tooltip.

    The number is given, not only the colour. A colour asserts a bucket with
    no room for doubt, and this distance is derived from an absolute
    magnitude that is itself an estimate -- half a magnitude of error is a
    quarter of the distance. Someone deciding whether to point at a thing
    should be able to see 0.043 and judge for themselves.
    """
    d = mark.get("delta_au")
    if d is None:
        return " &#8212; distance not derivable"
    word = {neodistance.NEAR: "within 0.01 AU",
            neodistance.CLOSE: "0.01&#8211;0.05 AU",
            neodistance.MAIN_BELT: "beyond 0.05 AU, main-belt orbit",
            neodistance.FAR: "beyond 0.05 AU"}.get(mark.get("distance"), "")
    return f" &#8212; about {d:.4f} AU from Earth ({word})"


def _distance_at(track, ts):
    """Interpolated geocentric distance, or None where it is not derivable.

    Separate from _interpolate() so the position path keeps its shape and
    every caller of it keeps working. Either endpoint missing a distance
    means None rather than a value carried across the gap: the derivation
    refuses some samples on purpose, and quietly substituting a neighbour's
    answer would paint a colour for geometry we declined to judge.
    """
    if not track or len(track[0]) < 4:
        return None
    if ts <= track[0][0]:
        return track[0][3]
    if ts >= track[-1][0]:
        return track[-1][3]
    for a, b in zip(track, track[1:]):
        if a[0] <= ts <= b[0]:
            if a[3] is None or b[3] is None:
                return None
            span = b[0] - a[0]
            f = 0.0 if span == 0 else (ts - a[0]) / span
            return a[3] + (b[3] - a[3]) * f
    return track[-1][3]


def _spacing(track):
    """Typical gap between ephemeris samples, in seconds."""
    if len(track) < 2:
        return 3600.0
    gaps = sorted(b[0] - a[0] for a, b in zip(track, track[1:]))
    return gaps[len(gaps) // 2] or 3600.0


def _covers(track, now, gap):
    """Does the ephemeris genuinely cover `now`?

    "Genuinely" is the whole point. A track is not one continuous run: MPC
    suppresses every row below the server floor, so a target that sets and
    rises again leaves a hole of hours in the middle, and the samples either
    side of that hole are still CONSECUTIVE in the list. Asking only whether
    the nearest sample is within `gap` of now -- which is what this replaced
    -- says yes for the whole half-hour after a run ends, and _interpolate()
    then blends that run's last sample with the next run's first one, which
    is frequently the following night. The marker barely moves for half an
    hour of scrub time and then teleports. Measured on the live board:
    P12q6iW azimuth 219 to 134 across 02:31, gb00870 242 to 127 across 04:01.

    So inside the track the bracketing pair's OWN gap has to be no wider than
    the typical spacing -- that is what distinguishes standing inside a
    covered run from standing in a hole between two of them.

    Off either END of the track the old tolerance is kept, because there is
    nothing to blend with: _interpolate() clamps to the end sample, so the
    worst it can say is a real position up to `gap` stale, which is the same
    bounded approximation every sample in the track already is. Tightening
    this end case as well would drop a track that holds a single sample --
    which is what an archived night with one ephemeris line is.
    """
    if now < track[0][0]:
        return track[0][0] - now <= gap
    if now > track[-1][0]:
        return now - track[-1][0] <= gap
    for a, b in zip(track, track[1:]):
        if a[0] <= now <= b[0]:
            # Landing exactly on a sample is coverage by definition, however
            # wide the hole on the far side of it: _interpolate() returns that
            # sample's own position with nothing blended into it. Without this
            # the last instant of a run whose next gap is a shade over the
            # median -- which is most runs' final pair -- loses its marker.
            if now == a[0] or now == b[0]:
                return True
            return b[0] - a[0] <= gap
    # Both ends are handled above, so reaching here means `now` sits between
    # the first sample and the last with no pair around it -- which only a
    # one-sample track can do, by landing exactly on it.
    return True


def target_marks(rows, tracks, now, site=None):
    """Queue rows plus cached tracks to map markers.

    A target is drawn on the disc only when the ephemeris actually covers this
    instant. MPC generates these with a 20-degree floor and suppresses the
    rest, so a gap means the object is not up -- it then gets a rim tick at
    the azimuth where it next appears, rather than a dot at a position it does
    not occupy. Silently plotting the nearest row instead is how a map ends up
    showing the whole night's targets at three in the afternoon.

    `site` decides which dome's limits the markers are filtered by. It used to
    be left off, so the wedges render_svg() draws came from the site being
    viewed while the markers were filtered against config.DEFAULT_SITE -- with
    one observatory configured those are the same object, and with two they
    are not.
    """
    s = _site(site)
    marks = []
    for i, r in enumerate(rows, start=1):
        track = tracks.get(r["desig"])
        if not track:
            continue
        gap = _spacing(track)
        # Distance at the instant being drawn, so a candidate closing on
        # Earth changes colour through a replay rather than wearing the one
        # it had when the cycle ran.
        delta = _distance_at(track, now)
        common = {
            "desig": r["desig"], "index": i,
            "observed": bool(r.get("observed")),
            "vmag": r.get("vmag"), "score": r.get("score"),
            "delta_au": delta,
            "distance": neodistance.colour(delta, r.get("mb_score")),
        }
        if _covers(track, now, gap):
            az, alt = _interpolate(track, now)
            # Inside a keep-out wedge the target is simply not drawn. It is
            # sky the mount must not be sent to, so a marker there is an
            # invitation to do exactly that. It reappears if it comes out.
            if observability.keepout_violation(az, alt, s):
                continue
            # Judged at the position being drawn, not from the stored peak
            # flag. The queue badge answers "is tonight's best moment in poor
            # sky"; a live map has to answer "is it in poor sky right now",
            # and for most targets those differ -- the peak deliberately
            # prefers clean sky, so a peak-derived ring would almost never
            # light up even while a target sat in the light dome.
            reason, _hard = observability.mask_violation(az, alt, s)
            marks.append(dict(common, up=True, az=az, alt=alt,
                              mask=bool(reason)))
            continue
        ahead = [p for p in track
                 if p[0] > now
                 and not observability.keepout_violation(p[1], p[2], s)]
        if ahead:
            reason, _hard = observability.mask_violation(ahead[0][1],
                                                         ahead[0][2], s)
            # How far ahead the rise is, so the label can say which night it
            # falls on. Past the end of a covered run the next sample is
            # frequently tomorrow's, and a bare "20:14" then reads as tonight.
            marks.append(dict(common, up=False, az=ahead[0][1],
                              alt=ahead[0][2], rise_ts=ahead[0][0],
                              rise_ahead_s=ahead[0][0] - now,
                              mask=bool(reason)))
    return marks


# --- rendering --------------------------------------------------------------

def _moon_locus(moon, cx, cy, radius, sep_deg=None, site=None):
    """The true locus of constant separation around the moon, as SVG paths.

    Radius here is linear in zenith distance, so distance from the centre is
    the one quantity the projection preserves: a locus around the zenith
    really is a circle, and the distortion grows toward the horizon. At 10
    degrees altitude the projected width varies by more than 40 percent, so a
    plain circle would be visibly wrong exactly where the moon usually is when
    it matters. Sampling the real locus gets the shape right. Segments that
    dip below the horizon are dropped rather than folded back over the disc.
    """
    sep = _site(site).moon_sep_min if sep_deg is None else sep_deg
    n = config.SKYMAP_MOON_RING_POINTS
    bearings = [360.0 * k / n for k in range(n + 1)]
    alts, azs = observability.offset_position(moon["alt"], moon["az"],
                                              sep, bearings)
    runs, cur = [], []
    for alt, az in zip(list(alts), list(azs)):
        if alt >= 0.0:
            cur.append(_polar(cx, cy, _radius_for(alt, radius), az))
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [" ".join(f"{x:.1f},{y:.1f}" for x, y in run)
            for run in runs if len(run) > 1]


def lune(illum, r):
    """Terminator geometry for a phase: (semi-axis, SVG sweep flag).

    The lit shape is a semicircle closed by a half-ellipse. The first arc runs
    top to bottom down the right-hand limb (sweep 1, clockwise). The second
    returns bottom to top: sweep 1 takes it round the LEFT and adds area,
    giving a gibbous or full disc; sweep 0 brings it back through the right
    and subtracts, giving a crescent.

    So the flag follows the phase, not the reverse -- inverting it draws a
    13-percent crescent as an 87-percent gibbous, which is what it did until a
    render of a real night showed a nearly full moon three days after new.

    The enclosed area works out to exactly `illum` of the disc, which is what
    test_moon_phase_geometry checks.
    """
    return abs(1.0 - 2.0 * illum) * r, (1 if illum > 0.5 else 0)


def lit_fraction(rx, sweep, r):
    """Inverse of lune(): the fraction of the disc the drawn shape covers.

    Half the disc, plus or minus the half-ellipse the closing arc sweeps.
    """
    half_ellipse = math.pi * r * rx / 2.0
    area = math.pi * r * r / 2.0 + (half_ellipse if sweep else -half_ellipse)
    return area / (math.pi * r * r)


def _moon_glyph(mx, my, r, illum):
    """Disc with the illuminated fraction shaded, as a lune.

    The terminator is always drawn vertical: this says how much of the moon
    is lit, not which way the bright limb points. The real position angle
    would need the sun's direction on the sky, and nothing here uses it -- the
    number in the tooltip is the fact, the glyph is the summary.
    """
    rx, sweep = lune(illum, r)
    return (f'<path d="M{mx:.1f} {my - r:.1f} A{r:.1f} {r:.1f} 0 0 1 '
            f'{mx:.1f} {my + r:.1f} A{rx:.1f} {r:.1f} 0 0 {sweep} '
            f'{mx:.1f} {my - r:.1f} Z" fill="#e8ecf5" fill-opacity=".92"/>')


def _rise_label(m, fmt, fmtdt):
    """A rise time, carrying its date whenever that date is not now's.

    Past a hole in an ephemeris the next sample is frequently the FOLLOWING
    night's, and a bare "03:00" on that tick is indistinguishable from a rise
    a few hours away -- an observer reads tomorrow's rise as tonight's and
    waits for an object that is not coming back. The date settles it.

    Compared through the caller's own formatter rather than against a fixed
    number of hours, because "a different day" is a fact about local midnight
    at the observatory, which only the formatter knows.
    """
    when = fmt(m["rise_ts"])
    ahead = m.get("rise_ahead_s")
    if ahead is None:
        return when
    here, there = fmtdt(m["rise_ts"] - ahead), fmtdt(m["rise_ts"])
    return there if here[:10] != there[:10] else when


# The group the moving half of the map lives in. The replay animation swaps
# this group's contents, frame by frame, and touches nothing else -- so the
# horizon mask, the keep-out wedges, the rings and the rose are parsed once
# per page rather than once per frame. Named here rather than spelled out in
# the template so the two cannot drift.
DYNAMIC_CLASS = "skydyn"


def _frame(size):
    """Canvas geometry shared by the backdrop and the moving layer."""
    size = size or config.SKYMAP_SIZE
    pad = 30
    return size, (size - 2 * pad) / 2.0, size / 2.0, size / 2.0


def backdrop_svg(size=None, site=None):
    """Everything on the map that does not depend on the time.

    The disc, the horizon mask, the keep-out wedges, the altitude rings and
    the compass rose are all statements about where the telescope is, not
    about when it is looking -- so they are the same picture at every instant
    of the night, and the replay reuses one copy of them instead of shipping
    them 240 times.

    Opens the <svg> and deliberately does not close it: render_svg() closes
    it after the moving layer, so the bytes it returns are exactly the two
    halves joined. Anything else here would let the replay's backdrop and the
    live route's backdrop drift apart.
    """
    s = _site(site)
    size, radius, cx, cy = _frame(size)

    p = [f'<svg viewBox="0 0 {size} {size}" width="100%" '
         f'style="max-width:{size}px;display:block;margin:0 auto" role="img" '
         f'aria-label="All-sky map of tonight\'s targets, north up, east right">',
         # Hatching for the keep-out wedges. Deliberately a different visual
         # language from the flat pink of the advisory mask: one means poor
         # sky, the other means the mount can be damaged, and they must not be
         # mistaken for each other at a glance in a dark dome.
         '<defs><pattern id="keepout" width="7" height="7" '
         'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
         '<rect width="7" height="7" fill="rgba(207,97,84,.13)"/>'
         '<line x1="0" y1="0" x2="0" y2="7" stroke="#cf6154" '
         'stroke-opacity=".55" stroke-width="2"/></pattern></defs>',
         f'<circle cx="{cx}" cy="{cy}" r="{radius:.1f}" fill="#080a11" '
         f'stroke="#242b3a"/>']

    # --- horizon mask, drawn rather than enforced ---
    for idx, (a0, a1, minalt, hardness, why) in enumerate(s.horizon_mask):
        r_in = 0.0 if minalt is None else _radius_for(minalt, radius)
        hard = hardness == "hard"
        fill = "rgba(207,97,84,.20)" if hard else (
            "rgba(207,97,84,.15)" if minalt is None else "rgba(207,97,84,.085)")
        # The sector's own reason rather than a guess: "light pollution" was
        # true of L01's north and is not a fact about anybody else's sky.
        label = ("cannot point" if hard else
                 "discouraged at every altitude" if minalt is None else
                 f"below {minalt:.0f}&#176;")
        if why:
            label += f" &#8212; {escape(str(why))}"
        p.append(f'<path d="{_wedge_path(cx, cy, radius, r_in, a0, a0 + _SECTOR_WIDTH)}" '
                 f'fill="{fill}"><title>{escape(str(s.sector_names[idx]))} &#8212; '
                 f'{label}</title></path>')

    # --- keep-out wedges, over the advisory mask and under everything else ---
    for start, end, min_alt, reason in s.keepout_wedges:
        span = (end - start) % 360.0
        r_in = _radius_for(min_alt, radius)
        p.append(f'<path d="{_wedge_path(cx, cy, radius, r_in, start, start + span)}" '
                 f'fill="url(#keepout)" stroke="#cf6154" stroke-opacity=".5" '
                 f'stroke-width="1.2"><title>Keep out &#8212; '
                 f'{escape(str(reason))}. '
                 f'Below {min_alt:.0f}&#176; between azimuth {start:.0f}&#176; '
                 f'and {end:.0f}&#176;, the telescope must not be pointed here.'
                 f'</title></path>')

    # --- altitude rings and the compass rose ---
    for alt in config.SKYMAP_RINGS:
        rr = _radius_for(alt, radius)
        p.append(f'<circle cx="{cx}" cy="{cy}" r="{rr:.1f}" fill="none" '
                 f'stroke="#1b2230"/>')
        p.append(f'<text x="{cx + 3:.1f}" y="{cy - rr + 10:.1f}" fill="#4a5468" '
                 f'font-size="8.5" font-family="monospace">{alt}&#176;</text>')

    for idx, name in enumerate(s.sector_names):
        az = idx * _SECTOR_WIDTH
        x0, y0 = _polar(cx, cy, radius, az)
        p.append(f'<line x1="{cx}" y1="{cy}" x2="{x0:.1f}" y2="{y0:.1f}" '
                 f'stroke="#161c28"/>')
        lx, ly = _polar(cx, cy, radius + 15, az)
        weight = "700" if len(name) == 1 else "500"
        p.append(f'<text x="{lx:.1f}" y="{ly + 3.5:.1f}" fill="#808b9d" '
                 f'font-size="{10.5 if len(name) == 1 else 9}" font-weight="{weight}" '
                 f'text-anchor="middle" font-family="monospace">{name}</text>')

    return "".join(p)


def dynamic_svg(marks, moon=None, size=None, localt=None, site=None,
                localdt=None):
    """Only the half of the map that moves: the moon and the markers.

    One of these is a replay frame. `marks` must already be what
    target_marks() decided -- which of them are drawn at all, which carry the
    poor-sky ring, which are rim ticks -- because those are this
    observatory's own limits applied position by position, and nothing
    downstream of here is allowed to re-decide them. See the module note on
    target_marks() and skyframes.py.
    """
    s = _site(site)
    size, radius, cx, cy = _frame(size)
    fmt = localt or (lambda ts: "")
    fmtdt = localdt or (lambda ts: "")
    p = []

    # --- moon ---
    if moon and moon["alt"] > 0:
        for pts in _moon_locus(moon, cx, cy, radius, site=s):
            p.append(f'<polyline points="{pts}" fill="none" stroke="#5fc9d4" '
                     f'stroke-opacity=".55" stroke-dasharray="4 4"/>')
        mx, my = project(moon["az"], moon["alt"], cx, cy, radius)
        p.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="9" fill="#0a0c14" '
                 f'stroke="#5fc9d4" stroke-opacity=".5"/>')
        p.append(_moon_glyph(mx, my, 8.0, moon["illum"]))
        p.append(f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="10" fill="none" '
                 f'pointer-events="all"><title>Moon &#8212; '
                 f'{moon["illum"] * 100:.0f}% illuminated, altitude '
                 f'{moon["alt"]:.0f}&#176;, azimuth {moon["az"]:.0f}&#176;. '
                 f'Dashed line is {s.moon_sep_min:.0f}&#176; separation.'
                 f'</title></circle>')

    # --- targets ---
    for m in marks:
        colour = _mark_colour(m)
        if m["up"]:
            x, y = project(m["az"], m["alt"], cx, cy, radius)
            p.append(f'<a href="/target/{m["desig"]}" class="mk" '
                     f'data-desig="{m["desig"]}">')
            p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" '
                     f'fill="{colour}" fill-opacity=".9" stroke="#080a11" '
                     f'stroke-width="1.2"/>')
            if m["mask"]:
                p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="9" fill="none" '
                         f'stroke="{colour}" stroke-opacity=".45" '
                         f'stroke-dasharray="2 2"/>')
            p.append(f'<text x="{x + 9:.1f}" y="{y - 6:.1f}" fill="{colour}" '
                     f'font-size="9.5" font-family="monospace">{m["index"]}</text>')
            p.append(f'<title>{m["desig"]} &#8212; altitude {m["alt"]:.0f}&#176;, '
                     f'azimuth {m["az"]:.0f}&#176;, V {m["vmag"]:.1f}, '
                     f'score {m["score"]}'
                     f'{_distance_phrase(m)}'
                     f'{" &#8212; in light-polluted sky" if m["mask"] else ""}'
                     f'{" &#8212; observed" if m["observed"] else ""}</title>')
            p.append("</a>")
        else:
            x0, y0 = _polar(cx, cy, radius + 1, m["az"])
            x1, y1 = _polar(cx, cy, radius + 8, m["az"])
            tx, ty = _polar(cx, cy, radius + 8, m["az"])
            p.append(f'<a href="/target/{m["desig"]}" class="mk tick" '
                     f'data-desig="{m["desig"]}">')
            p.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" '
                     f'y2="{y1:.1f}" stroke="{colour}" stroke-opacity=".75" '
                     f'stroke-width="2"/>')
            p.append(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="7" fill="none" '
                     f'pointer-events="all"/>')
            p.append(f'<title>{m["desig"]} &#8212; not yet up, first above '
                     f'{s.mpc_server_min_alt:.0f}&#176; at '
                     f'{_rise_label(m, fmt, fmtdt)} toward azimuth '
                     f'{m["az"]:.0f}&#176;</title>')
            p.append("</a>")

    return "".join(p)


def render_svg(marks, moon=None, size=None, localt=None, site=None,
               localdt=None):
    """The whole map. `localt` formats a unix timestamp for rise labels, and
    `localdt` the same instant with its date, for a rise on another night.

    Literally the backdrop followed by one moving layer, which is what lets
    the replay precompute only the second half: a frame the updater stored is
    the same bytes this function would have put inside that group, so the
    animated map cannot show anything the live route would not.
    """
    return (backdrop_svg(size, site)
            + f'<g class="{DYNAMIC_CLASS}">'
            + dynamic_svg(marks, moon, size=size, localt=localt, site=site,
                          localdt=localdt)
            + "</g></svg>")
