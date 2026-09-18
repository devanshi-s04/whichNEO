"""Observability calculations for one observatory.

Every function that depends on where the telescope is takes a `site`. Passing
none means the deployment's default site, which is how the single-observatory
board calls it -- see sites.py.
"""

import time

import numpy as np
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord, AltAz, TETE, get_body
from astropy.utils import iers

import config

# An unattended 5-minute service must never block on a network fetch for earth
# orientation data, and stale predictive tables otherwise raise outright. The
# resulting error is far below a degree, which is irrelevant for horizon flags.
iers.conf.auto_download = False
iers.conf.auto_max_age = None
iers.conf.iers_degraded_accuracy = "ignore"

_SIDEREAL_RATE = 1.0027379093


def _site(site):
    """The site to work on. None means the deployment's default.

    This is the one place in the module that still reaches for a global, so
    when several sites exist it is the one line to delete.
    """
    return config.DEFAULT_SITE if site is None else site


def earth_location(site=None):
    """The site as an EarthLocation, built from its MPC parallax constants."""
    return _site(site).earth_location


def sector_index(az_deg):
    """Index into a site's horizon mask for an azimuth.

    Sectors are 45 deg wide starting at 337.5, matching the mask's own order,
    so the lookup is direct arithmetic rather than a search. That layout is a
    property of the mask format rather than of any one observatory, so this
    needs no site.
    """
    return np.floor(((np.asarray(az_deg) - 337.5) % 360.0) / 45.0).astype(int)


def min_altitude_for_azimuth(az_deg, site=None):
    """Horizon-mask floor for a given azimuth. inf means the whole sector is
    discouraged at every altitude."""
    return _site(site).min_alt_by_sector[sector_index(az_deg)]


def sector_is_hard(az_deg, site=None):
    """True where the sector is a real obstruction rather than a preference."""
    return _site(site).hard_by_sector[sector_index(az_deg)]


def hard_min_altitude(az_deg, site=None):
    """The altitude floor that actually rejects, per azimuth.

    Soft sectors impose none (-inf), so a discouraged direction never
    shortens a computed observing window -- it only earns a warning.
    """
    s = _site(site)
    idx = sector_index(az_deg)
    return np.where(s.hard_by_sector[idx], s.min_alt_by_sector[idx], -np.inf)


def in_arc(az_deg, start_deg, end_deg):
    """Is an azimuth inside the arc running CLOCKWISE from start to end?

    Clockwise matters: (270, 45) is west through north to north-east, not the
    long way round through south. Both are "W to NE" in English.
    """
    return ((np.asarray(az_deg) - start_deg) % 360.0) <= ((end_deg - start_deg) % 360.0)


def keepout_violation(az_deg, alt_deg, site=None):
    """The keep-out wedge this position falls in, or None.

    Unlike the horizon mask this is never advisory: a site's keep-out wedges
    describe sky that would put the telescope somewhere it can be damaged,
    so a hit here is a refusal with no override.
    """
    for start, end, min_alt, reason in _site(site).keepout_wedges:
        if bool(in_arc(az_deg, start, end)) and alt_deg < min_alt:
            return reason
    return None


def mask_violation(az_deg, alt_deg, site=None):
    """How a single position sits against the mask.

    Returns (reason, hard) where reason is None when the position is clear.
    A soft violation is information, not a rejection -- it must never be used
    to drop a target, only to warn about one.
    """
    s = _site(site)
    idx = int(sector_index(az_deg))
    floor = float(s.min_alt_by_sector[idx])
    hard = bool(s.hard_by_sector[idx])
    if np.isinf(floor):
        return "azBlocked", hard
    if alt_deg < floor:
        return "belowMask", hard
    return None, hard


def _altaz_from_hour_angle(ha_deg, dec_deg, lat_deg):
    """Fast analytic alt/az. Used only for the 24h scan, where sub-degree
    accuracy is irrelevant; instantaneous values use the full astropy path."""
    ha = np.radians(ha_deg)
    dec = np.radians(dec_deg)
    lat = np.radians(lat_deg)

    sin_alt = np.sin(dec) * np.sin(lat) + np.cos(dec) * np.cos(lat) * np.cos(ha)
    alt = np.arcsin(np.clip(sin_alt, -1.0, 1.0))

    denom = np.cos(alt) * np.cos(lat)
    cos_az = np.where(np.abs(denom) < 1e-12, 1.0,
                      (np.sin(dec) - np.sin(alt) * np.sin(lat)) / np.where(
                          np.abs(denom) < 1e-12, 1.0, denom))
    az = np.degrees(np.arccos(np.clip(cos_az, -1.0, 1.0)))
    az = np.where(np.sin(ha) > 0, 360.0 - az, az)
    return np.degrees(alt), az


def airmass(alt_deg):
    if alt_deg <= 0:
        return None
    return float(1.0 / np.cos(np.radians(90.0 - alt_deg)))


def moon_illumination(t, loc):
    sun = get_body("sun", t, loc)
    moon = get_body("moon", t, loc)
    elong = sun.separation(moon)
    phase = np.arctan2(sun.distance * np.sin(elong),
                       moon.distance - sun.distance * np.cos(elong))
    return float((1 + np.cos(phase)) / 2.0)


# Bounded on purpose, and cleared rather than evicted one at a time. The key
# carries a timestamp, so an open-ended cache would grow a fresh entry every
# minute the updater stays up -- days of them for a process that is meant to
# run unattended for months. A few hundred entries covers every instant of a
# night at both sites, which is the access pattern that matters: the replay
# slider asks for the same minutes over and over as the handle moves.
_MOON_CACHE_MAX = 512
_MOON_CACHE = {}


def moon_state(unix_ts=None, site=None):
    """Where the moon is and how full it is, for the sky map.

    Azimuth is returned as a compass bearing, the same convention the horizon
    mask and the parsed ephemeris rows use. compute() below calculates the
    moon's altitude for the filter cascade and used to discard the azimuth;
    the map needs both, so this returns the pair.

    Memoised to the minute, per site. This is the single most expensive thing
    the sky map does -- measured at 43.8 ms of a frame that costs 62 ms end to
    end, all of it astropy -- and it dominates every frame the replay slider
    asks for.

    A minute of rounding cannot show. What moves here is the moon's APPARENT
    place, which the earth's rotation carries about 0.25 degrees per minute;
    on a 560 px disc spanning 90 degrees of zenith distance that is under a
    pixel, and rounding to the nearest minute is half of it. The separation
    locus is drawn around the same position, so it shifts by the same amount.
    Rounding also bounds how stale a live request can be, at one minute,
    rather than leaving it open the way a keyed-on-request cache would.

    Callers get a copy, because `sky_view` hands this dict on to the template
    and to render_svg; one caller mutating it would otherwise poison the
    entry for every later request.
    """
    s = _site(site)
    ts = float(unix_ts) if unix_ts is not None else time.time()
    # Quantise the timestamp, not just the key, so the cached answer is the
    # answer for the minute it is filed under rather than for whichever
    # request happened to arrive first inside it.
    minute = round(ts / 60.0)
    key = (minute, s.id)
    hit = _MOON_CACHE.get(key)
    if hit is None:
        loc = s.earth_location
        t = Time(minute * 60.0, format="unix")
        moon = get_body("moon", t, loc)
        aa = moon.transform_to(AltAz(obstime=t, location=loc))
        hit = {"alt": float(aa.alt.deg), "az": float(aa.az.deg),
               "illum": moon_illumination(t, loc), "ts": float(t.unix)}
        if len(_MOON_CACHE) >= _MOON_CACHE_MAX:
            _MOON_CACHE.clear()
        _MOON_CACHE[key] = hit
    return dict(hit)


def moon_altitudes(unix_ts_list, site=None):
    """Moon altitude at each of several instants, in one vectorised call.

    Used to draw a gap-free Moon curve on the altitude plot: the object's
    own ephemeris can have a real hole in it where MPC's floor cuts it off,
    but the Moon needs no orbit fit -- its position is exactly knowable for
    any instant -- so calling this once for a whole night's worth of sample
    times is both correct and cheap, unlike calling moon_state() in a loop
    (one Time object and one body transform per instant instead of one for
    the whole batch).
    """
    if not unix_ts_list:
        return []
    loc = _site(site).earth_location
    t = Time(np.asarray(unix_ts_list, dtype=float), format="unix")
    aa = get_body("moon", t, loc).transform_to(AltAz(obstime=t, location=loc))
    alts = np.atleast_1d(aa.alt.deg)
    return [(float(ts), float(alt)) for ts, alt in zip(unix_ts_list, alts)]


def angular_separation(alt1, az1, alt2, az2):
    """Great-circle separation between two horizon positions, in degrees.

    On the alt/az sphere altitude plays the part of latitude and azimuth of
    longitude, so this is the ordinary spherical distance. Used to place the
    lunar exclusion locus and to verify that it really is MOON_SEP_MIN wide.
    """
    a1, a2 = np.radians(alt1), np.radians(alt2)
    d_az = np.radians(np.asarray(az2) - np.asarray(az1))
    cos_d = np.sin(a1) * np.sin(a2) + np.cos(a1) * np.cos(a2) * np.cos(d_az)
    return np.degrees(np.arccos(np.clip(cos_d, -1.0, 1.0)))


def offset_position(alt_deg, az_deg, sep_deg, bearing_deg):
    """The point `sep_deg` away from (alt, az) along a given bearing.

    The standard destination-point formula on the alt/az sphere. Sampling
    bearings all the way round traces the true locus of constant separation,
    which is what the moon circle on the sky map has to be: a fixed angular
    radius is NOT a fixed radius on the projected disc, and drawing it as a
    plain circle would be wrong everywhere except directly overhead.
    """
    lat = np.radians(alt_deg)
    d = np.radians(sep_deg)
    brg = np.radians(np.asarray(bearing_deg, dtype=float))

    sin_lat2 = np.sin(lat) * np.cos(d) + np.cos(lat) * np.sin(d) * np.cos(brg)
    lat2 = np.arcsin(np.clip(sin_lat2, -1.0, 1.0))
    d_lon = np.arctan2(np.sin(brg) * np.sin(d) * np.cos(lat),
                       np.cos(d) - np.sin(lat) * np.sin(lat2))
    return np.degrees(lat2), (az_deg + np.degrees(d_lon)) % 360.0


def altaz_batch(ra_degs, dec_degs, unix_ts, site=None):
    """Alt/az and lunar separation for many positions at one instant.

    Used to verify MPC's ephemeris independently. Vectorised deliberately:
    called once per update rather than once per target.
    """
    if not len(ra_degs):
        return []
    loc = _site(site).earth_location
    t = Time(float(unix_ts), format="unix")
    frame = AltAz(obstime=t, location=loc)
    coords = SkyCoord(ra=np.asarray(ra_degs) * u.deg,
                      dec=np.asarray(dec_degs) * u.deg)
    aa = coords.transform_to(frame)
    moon = get_body("moon", t, loc)
    sep = coords.transform_to(moon.frame).separation(moon).deg
    alt = np.atleast_1d(aa.alt.deg)
    az = np.atleast_1d(aa.az.deg)
    sep = np.atleast_1d(sep)
    return [{"alt": float(alt[i]), "az": float(az[i]), "moon_sep": float(sep[i])}
            for i in range(len(alt))]


def compute(rows, when=None, scan_hours=24, scan_step_min=2, site=None):
    """Annotate each NEOCP row with observability from a given site.

    Instantaneous values use a full astropy transform; the forward scan that
    yields observing windows uses the analytic path so the whole update stays
    well inside the five-minute budget.
    """
    if not rows:
        return rows

    s = _site(site)
    loc = s.earth_location
    lat = loc.lat.deg
    now = Time(when) if when is not None else Time.now()

    coords = SkyCoord(ra=[r["ra_deg"] for r in rows] * u.deg,
                      dec=[r["dec_deg"] for r in rows] * u.deg)

    frame_now = AltAz(obstime=now, location=loc)
    aa = coords.transform_to(frame_now)
    alt_now = np.atleast_1d(aa.alt.deg)
    az_now = np.atleast_1d(aa.az.deg)

    sun_now = get_body("sun", now, loc)
    moon_now = get_body("moon", now, loc)
    sun_alt_now = float(sun_now.transform_to(frame_now).alt.deg)
    moon_aa_now = moon_now.transform_to(frame_now)
    moon_alt_now = float(moon_aa_now.alt.deg)
    # Kept, not discarded: the sky map draws the moon and its exclusion locus,
    # both of which need the azimuth as well as the altitude.
    moon_az_now = float(moon_aa_now.az.deg)
    illum = moon_illumination(now, loc)
    # Separate in the bodies' own topocentric GCRS frame. Mixing ICRS targets
    # with GCRS bodies makes astropy warn that the result is direction
    # dependent, and it matters here: lunar parallax reaches ~1 deg, so the
    # topocentric separation is the one an observer actually sees.
    coords_topo = coords.transform_to(moon_now.frame)
    moon_sep = coords_topo.separation(moon_now).deg
    sun_elong = coords_topo.separation(sun_now).deg

    lst_now = now.sidereal_time("apparent", longitude=loc.lon).deg

    # The analytic scan pairs hour angle with apparent sidereal time, so it
    # needs apparent (equinox-of-date) coordinates. Feeding it J2000 directly
    # leaves ~27 years of precession in, worth up to ~0.7 deg of azimuth --
    # enough to land a target in the wrong horizon-mask sector near a boundary.
    apparent = coords.transform_to(TETE(obstime=now))
    ra_app = np.atleast_1d(apparent.ra.deg)
    dec_app = np.atleast_1d(apparent.dec.deg)

    # Forward scan grid, shared by every target.
    n_steps = int(scan_hours * 60 / scan_step_min)
    minutes = np.arange(n_steps) * scan_step_min
    lst_grid = (lst_now + minutes * (360.0 / 1440.0) * _SIDEREAL_RATE) % 360.0
    grid_times = now + minutes * u.min
    sun_alt_grid = np.atleast_1d(
        get_body("sun", grid_times, loc).transform_to(
            AltAz(obstime=grid_times, location=loc)).alt.deg)
    dark_grid = sun_alt_grid < s.sun_alt_max

    for i, r in enumerate(rows):
        a, z = float(alt_now[i]), float(az_now[i])
        r["alt_deg"] = a
        r["az_deg"] = z
        r["airmass"] = airmass(a)
        r["sun_alt_deg"] = sun_alt_now
        r["moon_sep_deg"] = float(moon_sep[i])
        r["moon_alt_deg"] = moon_alt_now
        r["moon_az_deg"] = moon_az_now
        r["moon_illum"] = illum
        r["sun_elong_deg"] = float(sun_elong[i])

        # Hour angle is defined against apparent place, not J2000.
        ha = ((lst_now - ra_app[i] + 180.0) % 360.0) - 180.0
        r["hour_angle_deg"] = ha
        r["pre_meridian"] = ha < 0
        r["rising"] = ha < 0

        # Sidereal hours to transit, converted to civil minutes.
        hours_to_transit = ((ra_app[i] - lst_now) % 360.0) / 15.0
        r["minutes_to_transit"] = hours_to_transit * 60.0 / _SIDEREAL_RATE
        r["transit_utc"] = (now + r["minutes_to_transit"] * u.min).iso[:16] + " UT"

        alt_grid, az_grid = _altaz_from_hour_angle(
            ((lst_grid - ra_app[i] + 180.0) % 360.0) - 180.0, dec_app[i], lat)
        # Only hard limits close a window. A soft sector is poor sky, not
        # unreachable sky, so it must not shorten what we report as available.
        ok_grid = (alt_grid >= hard_min_altitude(az_grid, s)) & dark_grid
        # Keep-out wedges do close it: time the telescope cannot be pointed is
        # not time available, so it must not be counted as a window.
        for start, end, min_alt, _reason in s.keepout_wedges:
            ok_grid &= ~(in_arc(az_grid, start, end) & (alt_grid < min_alt))
        if s.max_altitude is not None:
            ok_grid &= alt_grid <= s.max_altitude

        flags = evaluate_flags(r, s)
        r["flags"] = flags
        r["observable"] = not flags

        if r["observable"]:
            end = np.argmax(~ok_grid) if (~ok_grid).any() else n_steps
            r["window_remaining_min"] = float(end * scan_step_min)
            r["minutes_until_observable"] = 0.0
        else:
            r["window_remaining_min"] = 0.0
            start = np.argmax(ok_grid) if ok_grid.any() else None
            r["minutes_until_observable"] = (
                float(start * scan_step_min) if start is not None else None)

    return rows


def evaluate_flags(r, site=None):
    """Reasons this target cannot be observed right now. Empty means go."""
    s = _site(site)
    flags = []
    if r["vmag"] > s.max_mag:
        flags.append("TOO_FAINT")
    if r["sun_alt_deg"] >= s.sun_alt_max:
        flags.append("SUN_UP")

    if keepout_violation(r["az_deg"], r["alt_deg"], s):
        flags.append("KEEP_OUT")

    # A mask violation only refuses when the sector is hard. Soft sectors
    # record a warning instead, so nothing is ever dropped for poor sky.
    reason, hard = mask_violation(r["az_deg"], r["alt_deg"], s)
    if reason and hard:
        flags.append("AZ_BLOCKED" if reason == "azBlocked" else "TOO_LOW")
    elif reason:
        r["mask_warning"] = reason
    if s.max_altitude is not None and r["alt_deg"] > s.max_altitude:
        flags.append("TOO_HIGH")

    if r["moon_sep_deg"] < s.moon_sep_min and r["moon_alt_deg"] > 0:
        flags.append("MOON_CLOSE")
    if r["desig"] in s.blacklist:
        flags.append("BLACKLISTED")
    return flags
