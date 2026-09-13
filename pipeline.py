"""Combine the three MPC sources into analysed, filtered targets.

Sources per object:
  * neocp.txt      -- designation, digest2 score, arc, not-seen, nobs
  * neocp_info     -- q, e, a, inclination
  * confirmeph2    -- the night's ephemeris rows, computed by MPC for L01

Ordering matters: ephemeris rows are screened for observability first, and the
maximum-altitude row is then the best *observable* moment, not the object's
astronomical peak (which is often in daylight). The legacy planner does the
same thing; getting it backwards would schedule targets for noon.
"""

import time

import config
import ephemeris
import observability


def night_bounds(now=None):
    """The observing night containing `now`, as (start_ts, end_ts) UTC.

    A night runs from NIGHT_ROLLOVER_HOUR_UT to the same hour the next day, so
    a session spanning midnight stays a single unit.
    """
    now = now if now is not None else time.time()
    t = time.gmtime(now)
    day_start = now - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec)
    start = day_start + config.NIGHT_ROLLOVER_HOUR_UT * 3600
    if now < start:
        start -= 86400
    return start, start + 86400


def night_label(now=None):
    start, _ = night_bounds(now)
    return time.strftime("%Y-%m-%d", time.gmtime(start))


def row_rejections(row, night_end_ts):
    """Why this ephemeris row is not observable. Empty means it is."""
    out = []
    if row.sun_alt > config.SUN_ALT_MAX:
        out.append("nearSun")
    if row.alt < config.MIN_ALT:
        out.append("altLow")
    if row.moon_dist < config.MOON_SEP_MIN:
        out.append("nearMoon")
    if row.ts > night_end_ts:
        out.append("tooLate")
    if row.motion < config.MIN_MOTION:
        out.append("tooSlow")

    floor = float(observability.min_altitude_for_azimuth(row.az))
    if floor == float("inf"):
        out.append("azBlocked")
    elif row.alt < floor:
        out.append("belowMask")
    if config.MAX_ALTITUDE is not None and row.alt > config.MAX_ALTITUDE:
        out.append("tooHigh")
    return out


def usable_rows(eph, night_end_ts):
    keep, report = [], {}
    for r in eph.rows:
        reasons = row_rejections(r, night_end_ts)
        if reasons:
            report[reasons[0]] = report.get(reasons[0], 0) + 1
        else:
            keep.append(r)
    return keep, report


def analyze(target, eph, orbit, now=None):
    """Annotate one target in place with ephemeris-derived fields and the
    reasons, if any, that it should not be observed tonight."""
    now = now if now is not None else time.time()
    _, night_end = night_bounds(now)
    discard = []

    target["q"] = orbit.get("q") if orbit else None
    target["e"] = orbit.get("e") if orbit else None
    target["incl"] = orbit.get("incl") if orbit else None

    target["eph_error"] = eph.error if eph else "not fetched"
    target["map_url"] = eph.map_url if eph else None
    target["offsets_url"] = eph.offsets_url if eph else None

    rows, report = usable_rows(eph, night_end) if eph else ([], {})
    target["eph_rows_total"] = len(eph.rows) if eph else 0
    target["eph_rows_usable"] = len(rows)
    target["eph_report"] = report

    # --- object-level filters, cheapest first ---
    if target["score"] < config.MIN_SCORE:
        discard.append("LOW_SCORE")
    if target["arc_days"] < config.MIN_ARC_DAYS:
        discard.append("SHORT_ARC")
    if target["not_seen_days"] > config.MAX_NOT_SEEN_DAYS:
        discard.append("NOT_SEEN")
    if target["desig"] in config.BLACKLIST:
        discard.append("BLACKLISTED")

    if config.NEO_ONLY and target["q"] is not None and target["e"] is not None:
        if not (target["q"] < config.NEO_Q_MAX or target["e"] > config.NEO_E_MIN):
            discard.append("NOT_NEO")

    sc = target.get("scatteredness")
    if sc:
        target["scattered_warn"] = (sc[0] > config.SCATTEREDNESS_WARN[0]
                                    or sc[1] > config.SCATTEREDNESS_WARN[1])
        if (sc[0] > config.MAX_SCATTEREDNESS[0]
                or sc[1] > config.MAX_SCATTEREDNESS[1]):
            discard.append("TOO_SCATTERED")

    if config.SKIP_ALREADY_OBSERVED and target.get("observed_from_site"):
        discard.append("ALREADY_OBSERVED")

    # --- ephemeris-level ---
    if not rows:
        discard.append("NO_WINDOW")
        target.update(max_alt_row=None, nearest_row=None, interp_row=None,
                      max_alt_ts=None, max_alt=None, exposure_min=None,
                      window_minutes=0.0, window_start_ts=None,
                      window_end_ts=None, mpc_flag=None)
    else:
        # MPC marks fast-moving ephemeris rows with ! or !!. It is a per-row
        # property that can change through a night, so the object-level badge
        # is the strongest marker over the rows we could actually observe.
        target["mpc_flag"] = ("!!" if any(r.flag == "!!" for r in rows)
                              else "!" if any(r.flag == "!" for r in rows)
                              else None)

        best = max(rows, key=lambda r: r.alt)
        target["max_alt_row"] = best
        target["max_alt_ts"] = best.ts
        target["max_alt"] = best.alt
        target["exposure_min"] = best.exposure_minutes()
        target["window_minutes"] = _contiguous_window(rows, now)
        # Span of the observable rows, for the night timeline.
        target["window_start_ts"] = min(r.ts for r in rows)
        target["window_end_ts"] = max(r.ts for r in rows)

        if best.vmag > config.MAX_MAG:
            discard.append("TOO_FAINT")

        at = now + config.INTERPOLATE_AHEAD_S
        target["nearest_row"] = min(rows, key=lambda r: abs(r.ts - at))
        # Interpolate within the observable rows only. Interpolating over the
        # raw set clamps to the first row of the ephemeris, which is often in
        # daylight, and emits a live pointing line with the sun up.
        target["interp_row"] = ephemeris.ObjectEphemeris(
            target["desig"], rows).interpolate_at(at)

    target["discard_reasons"] = discard
    target["observable"] = not discard
    return target


def _contiguous_window(rows, now):
    """Minutes of usable time remaining, from the ephemeris row spacing."""
    if not rows:
        return 0.0
    future = [r for r in rows if r.ts >= now]
    if not future:
        return 0.0
    ordered = sorted(future, key=lambda r: r.ts)
    step = 3600.0
    if len(ordered) > 1:
        step = min(b.ts - a.ts for a, b in zip(ordered, ordered[1:])) or 3600.0
    return round(len(ordered) * step / 60.0, 1)


def crosscheck_all(targets, at_ts):
    """Recompute MPC's alt/az/moon with astropy and flag disagreements.

    MPC stays the source of truth; this exists so a silent change in their
    page format surfaces as a flagged mismatch instead of quietly wrong sky
    positions. Only rows genuinely interpolated to `at_ts` are compared --
    rows clamped to the ends of an ephemeris refer to a different instant.
    """
    for t in targets:
        t.setdefault("crosscheck", None)
    if not config.CROSSCHECK:
        return targets

    todo = [t for t in targets
            if t.get("interp_row") is not None
            and abs(t["interp_row"].ts - at_ts) < 1.0]
    if not todo:
        return targets

    try:
        ours = observability.altaz_batch(
            [t["interp_row"].ra_deg for t in todo],
            [t["interp_row"].dec_deg for t in todo], at_ts)
    except Exception as e:
        for t in todo:
            t["crosscheck"] = {"error": f"{type(e).__name__}: {e}"}
        return targets

    for t, o in zip(todo, ours):
        row = t["interp_row"]
        d_alt = abs(o["alt"] - row.alt)
        d_az = abs(((o["az"] - row.az + 180) % 360) - 180)
        d_moon = abs(o["moon_sep"] - row.moon_dist)
        t["crosscheck"] = {
            "d_alt": round(d_alt, 2), "d_az": round(d_az, 2),
            "d_moon": round(d_moon, 2),
            "alt_ours": round(o["alt"], 2), "az_ours": round(o["az"], 2),
            "ok": (d_alt <= config.CROSSCHECK_ALT_TOL_DEG
                   and d_az <= config.CROSSCHECK_AZ_TOL_DEG
                   and d_moon <= config.CROSSCHECK_MOON_TOL_DEG),
        }
    return targets
