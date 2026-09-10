"""Observability calculations for L01 (Tican Station, Visnjan Observatory)."""

import numpy as np
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import EarthLocation, SkyCoord, AltAz, TETE, get_body
from astropy.utils import iers

import config

# An unattended 5-minute service must never block on a network fetch for earth
# orientation data, and stale predictive tables otherwise raise outright. The
# resulting error is far below a degree, which is irrelevant for horizon flags.
iers.conf.auto_download = False
iers.conf.auto_max_age = None
iers.conf.iers_degraded_accuracy = "ignore"

_A_EARTH_M = 6378137.0
_SIDEREAL_RATE = 1.0027379093

_MIN_ALT_BY_SECTOR = np.array(
    [np.inf if m is None else m for (_, _, m) in config.HORIZON_MASK]
)


def site():
    """L01 as an EarthLocation, built from its MPC parallax constants."""
    lon = np.radians(config.SITE_LON_DEG)
    return EarthLocation.from_geocentric(
        config.SITE_RHO_COS_PHI * _A_EARTH_M * np.cos(lon) * u.m,
        config.SITE_RHO_COS_PHI * _A_EARTH_M * np.sin(lon) * u.m,
        config.SITE_RHO_SIN_PHI * _A_EARTH_M * u.m,
    )


def min_altitude_for_azimuth(az_deg):
    """Horizon-mask floor for a given azimuth. inf means the sector is blocked.

    Sectors are 45 deg wide starting at 337.5, matching config.HORIZON_MASK
    order, so the sector index is a direct arithmetic lookup.
    """
    idx = np.floor(((np.asarray(az_deg) - 337.5) % 360.0) / 45.0).astype(int)
    return _MIN_ALT_BY_SECTOR[idx]


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


def compute(rows, when=None, scan_hours=24, scan_step_min=2):
    """Annotate each NEOCP row with observability for L01.

    Instantaneous values use a full astropy transform; the forward scan that
    yields observing windows uses the analytic path so the whole update stays
    well inside the five-minute budget.
    """
    if not rows:
        return rows

    loc = site()
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
    moon_alt_now = float(moon_now.transform_to(frame_now).alt.deg)
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
    dark_grid = sun_alt_grid < config.SUN_ALT_MAX

    for i, r in enumerate(rows):
        a, z = float(alt_now[i]), float(az_now[i])
        r["alt_deg"] = a
        r["az_deg"] = z
        r["airmass"] = airmass(a)
        r["sun_alt_deg"] = sun_alt_now
        r["moon_sep_deg"] = float(moon_sep[i])
        r["moon_alt_deg"] = moon_alt_now
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
        ok_grid = (alt_grid >= min_altitude_for_azimuth(az_grid)) & dark_grid
        if config.MAX_ALTITUDE is not None:
            ok_grid &= alt_grid <= config.MAX_ALTITUDE

        flags = evaluate_flags(r)
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


def evaluate_flags(r):
    """Reasons this target cannot be observed right now. Empty means go."""
    flags = []
    if r["vmag"] > config.MAX_MAG:
        flags.append("TOO_FAINT")
    if r["sun_alt_deg"] >= config.SUN_ALT_MAX:
        flags.append("SUN_UP")

    floor = float(min_altitude_for_azimuth(r["az_deg"]))
    if np.isinf(floor):
        flags.append("AZ_BLOCKED")
    elif r["alt_deg"] < floor:
        flags.append("TOO_LOW")
    if config.MAX_ALTITUDE is not None and r["alt_deg"] > config.MAX_ALTITUDE:
        flags.append("TOO_HIGH")

    if r["moon_sep_deg"] < config.MOON_SEP_MIN and r["moon_alt_deg"] > 0:
        flags.append("MOON_CLOSE")
    if r["desig"] in config.BLACKLIST:
        flags.append("BLACKLISTED")
    return flags
