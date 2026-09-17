"""Nightly plan file, in the format the observatory's own planner emits.

One file per observing night, e.g. plans/2026-09-10.txt:

    * CERQW32         score=74, obs=41, arc=1.68, notSeen=0.022days, \
obsExposure=22.5min, scatteredness=(2,1), q=0.953, e=0.633
       mapLink=http://cgi.minorplanetcenter.net/cgi-bin/uncertaintymap.cgi?...
    // 2026 09 08 1900   16 51 58.5 +53 37 53  83.2  20.5  2.89 071.3 124 +67 -16 0.07 090 -21
    // 2026 09 08 2130   16 52 44.9 +53 40 09  83.3  20.5  2.92 072.1 128 +45 -35 0.06 090 -28
    2026 09 08 2132   16 52 44.9 +53 40 09  83.3  20.5  2.92 072.1 128 +45 -35 0.06 090 -28

Three ephemeris lines per target: best observable altitude, nearest to now,
and the interpolated position. The first two are "//"-commented so exactly one
live line per target remains, which is what gets pointed at.

The legacy planner builds the interpolated line by splitting the original
string on single spaces and overwriting fixed indices, which corrupts it when
field widths shift -- its own output contains lines reading "128 128 +45 +45".
Here every line is formatted from the parsed values instead.
"""

import os

import config

HEADER = (
    'Date       UT   *  R.A. (J2000) Decl.  Elong.  V        Motion     '
    'Object     Sun         Moon\n'
    '                       h m                                      "/min   '
    'P.A.  Azi. Alt.  Alt.  Phase Dist. Alt.'
)


def _ra_hms(ra_deg):
    hours = (ra_deg % 360.0) / 15.0
    h = int(hours)
    m_f = (hours - h) * 60.0
    m = int(m_f)
    return h, m, (m_f - m) * 60.0


def _dec_dms(dec_deg):
    sign = "-" if dec_deg < 0 else "+"
    a = abs(dec_deg)
    d = int(a)
    m_f = (a - d) * 60.0
    m = int(m_f)
    return sign, d, m, (m_f - m) * 60.0


def ephemeris_line(row):
    """Render one ephemeris row at MPC's column positions."""
    t = row.utc
    rh, rm, rs = _ra_hms(row.ra_deg)
    sign, dd, dm, ds = _dec_dms(row.dec_deg)

    line = (
        f"{t.year:04d} {t.month:02d} {t.day:02d} {t.hour:02d}{t.minute:02d}   "
        f"{rh:02d} {rm:02d} {rs:04.1f} "
        f"{sign}{dd:02d} {dm:02d} {ds:02.0f}"
        f"{row.elong:6.1f}"
        f"{row.vmag:6.1f}"
        f"{row.motion:8.2f}"
        f"  {row.pa:05.1f}"
        f"  {int(round(row.az_mpc)) % 360:03d}"  # plan file keeps MPC's convention
        f"  {int(round(row.alt)):+03d}"
        f"   {int(round(row.sun_alt)):+03d}"
        f"{row.moon_phase:8.2f}"
        f"  {int(round(row.moon_dist)):03d}"
        f"  {int(round(row.moon_alt)):+03d}"
    )
    if row.flag:
        line += f"   {row.flag}"
    return line


def plan_entry(t):
    """One target block: summary line, map link, three ephemeris lines."""
    parts = [
        f"score={t['score']}",
        f"obs={t['nobs']}",
        f"arc={t['arc_days']}",
        f"notSeen={t['not_seen_days']}days",
    ]
    if t.get("exposure_min") is not None:
        parts.append(f"obsExposure={t['exposure_min']}min")
    sc = t.get("scatteredness")
    if sc:
        parts.append(f"scatteredness=({sc[0]},{sc[1]})")
    if t.get("q") is not None:
        parts.append(f"q={t['q']}")
    if t.get("e") is not None:
        parts.append(f"e={t['e']}")

    # The observatory's frame instruction, in the format Luka specified:
    #   * ZTF10G9 36 x 02 sec score=100, obs=4, ...
    # Seconds are zero-padded to two digits and the separators are single
    # spaces, exactly as he wrote it. NEOCP designations are all seven
    # characters, so the columns still line up without the old padding.
    if t.get("frames") and t.get("frame_sec"):
        head = f"* {t['desig']} {t['frames']} x {t['frame_sec']:02d} sec "
    else:
        head = f"* {t['desig']}         "
    out = [head + ", ".join(parts)]
    if t.get("map_url"):
        out.append(f"   mapLink={t['map_url']}")

    if t.get("max_alt_row") is not None:
        out.append("// " + ephemeris_line(t["max_alt_row"]))
    if t.get("nearest_row") is not None:
        out.append("// " + ephemeris_line(t["nearest_row"]))
    live = t.get("interp_row") or t.get("nearest_row")
    if live is not None:
        # The one uncommented line is what gets pointed at, so it must refer
        # to *now*. interpolate_at() clamps to the nearest usable row when now
        # falls outside the usable span, which happens constantly -- before a
        # target rises, and again once its window closes. The coordinates are
        # then right for a moment that is not this one, and the sky has turned
        # since: slewing to them points somewhere else entirely, which on a
        # mount with keep-out wedges is a way to hit one. Measured on a live
        # board, 4 of 24 targets were publishing such a line.
        #
        # Comment it out rather than drop it: the block keeps its shape, the
        # coordinates stay readable as a reference, and there is simply no
        # pointable line until the target really is pointable.
        pointable = t.get("live_row_is_now")
        out.append(ephemeris_line(live) if pointable
                   else "// " + ephemeris_line(live)
                        + "   <- not now, do not slew")
    return "\n".join(out)


def render_plan(targets):
    body = "\n\n".join(plan_entry(t) for t in targets if t.get("observable"))
    return HEADER + "\n\n\n\n" + body + "\n"


def write_plan(targets, night_label, directory=None, site=None):
    """Write <the site's plan_dir>/<night>.txt and return the path.

    Each site says where its own plans go, so an observatory that has driven
    a telescope from `plans/` for years keeps that path while a new one gets
    its own directory -- and no code here knows which is which.

    Refuses to replace a plan that has content with an empty one. Once a
    night ends nothing is observable any more, but the night label does not
    roll over until 11:00 UT -- so the cycles between dawn and rollover
    would otherwise overwrite the night's record with a bare header, which
    is exactly what happened to the first two nights.
    """
    if directory is None:
        s = config.DEFAULT_SITE if site is None else site
        directory = os.path.join(config.BASE_DIR, s.plan_dir)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{night_label}.txt")

    body = render_plan(targets)
    has_targets = any(t.get("observable") for t in targets)
    if not has_targets and os.path.exists(path):
        if os.path.getsize(path) > len(HEADER) + 8:
            return path

    with open(path, "w") as f:
        f.write(body)
    return path


def one_line_command(t):
    """Compact form for a copy button: name, frames, exposure."""
    mins = t.get("exposure_min")
    if mins is None:
        return t["desig"]
    return f"{t['desig']} {mins}min"
