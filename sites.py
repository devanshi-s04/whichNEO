"""A Site: one observatory, and everything whichNEO needs to know about it.

Until now every module reached for `config.<SOMETHING>` -- a module-level
singleton describing L01. That works exactly as long as there is one
observatory. This type is the same information passed as an argument instead
of read from a global, which is what lets a second observatory exist at all.

Nothing in this file knows about L01. The values come from config.py, which
builds the default site; see multisite.md (branch `multisite-plan`) for the
staged plan this is step A of.

Three kinds of setting live here, and the split is deliberate:

  site and telescope   obscode, parallax constants, timezone, field of view,
                       the horizon mask, the keep-out wedges, exposures
  observing policy     magnitude limit, moon separation, sun altitude, the
                       digest2 floor, motion floor, scatteredness limits
  (not here)           MPC endpoints, timeouts, cache schema, sky-map
                       geometry, storage paths, mail, accounts -- those are
                       properties of the deployment, not of an observatory,
                       and stay in config.py

The derived values at the bottom are computed once per site and cached: the
sector arrays are read on every vectorised alt/az scan, and the EarthLocation
is rebuilt for every ephemeris batch, so neither wants recomputing per call.
Their imports are deliberately lazy -- `manage.py` and the mailer construct a
Site by importing config, and must not pay for numpy and astropy to do it.
"""

import os
from dataclasses import dataclass, fields
from functools import cached_property

# Equatorial radius, the same figure MPC's parallax constants are expressed
# against. Only used to turn rho_cos_phi / rho_sin_phi back into metres.
A_EARTH_M = 6378137.0


@dataclass
class Site:
    """One observatory. Every field is supplied; nothing defaults to L01."""

    # --- Identity and geometry ---------------------------------------------
    # The site's identity in the database. Every per-site row carries it:
    # targets, ephemeris_cache, observer_state and night_archive are all
    # about an object *as seen from here*, while the NEOCP list, the ds42
    # scores and the history are about the object itself and stay shared.
    # L01 is site 1, by migration rather than by privilege.
    id: int
    # Parallax constants rather than a lat/lon, so the site matches exactly
    # what Find_Orb and the MPC use for this code.
    obscode: str
    # What the site switcher calls it. MPC's own name for the code is in
    # observatories.py; this is what the observatory calls itself.
    name: str
    # Where this site's nightly plan files are written, relative to the
    # repository. A setting rather than a rule, so that L01 can keep writing
    # to the `plans/` directory its legacy planner already reads while a new
    # observatory gets its own subdirectory -- no code anywhere knows which
    # of those is the special case.
    plan_dir: str
    lon_deg: float
    rho_cos_phi: float
    rho_sin_phi: float

    # --- Clock --------------------------------------------------------------
    # The board is read at the observatory, so times are shown in its local
    # time. The plan file stays UTC on purpose.
    display_tz: str
    # A night is one unit from this UT hour to the same hour next day, so a
    # session spanning midnight names one output file. Tuned per site: the
    # boundary wants to sit near local noon, not at a fixed UT hour.
    night_rollover_hour_ut: int

    # --- Horizon / dome mask -----------------------------------------------
    # (az_start, az_end, minimum observable altitude or None, hardness,
    # reason). "soft" keeps the target and flags it; "hard" rejects it
    # outright.
    #
    # The reason is not decoration. In six months "light pollution" versus
    # "the dome is there" is what tells someone whether a limit is safe to
    # relax, and that is exactly the judgement a settings page has to show
    # instead of losing. It used to live only as a comment beside the value,
    # and a comment is the one thing a settings form cannot edit.
    horizon_mask: tuple
    sector_names: tuple
    # (az_from, az_to, min_altitude, reason), the arc running CLOCKWISE from
    # az_from. Never advisory: a hit here is a refusal with no override.
    keepout_wedges: tuple
    # Flat floor applied in addition to the mask.
    min_alt: float
    # Zenith blind spot; None disables the check.
    max_altitude: float | None
    # Altitude floor handed to MPC as `oalt`, so the server never returns rows
    # below it. Note this dominates min_alt.
    mpc_server_min_alt: float

    # --- Telescope ----------------------------------------------------------
    # Square field used to overlay the uncertainty map.
    fov_arcsec: float
    # Ephemerides are interpolated this far ahead, so coordinates account for
    # the time it takes this mount to slew and settle.
    interpolate_ahead_s: int

    # --- Filter cascade -----------------------------------------------------
    max_mag: float
    sun_alt_max: float
    moon_sep_min: float
    min_score: int
    min_arc_days: float
    max_not_seen_days: float
    min_motion: float
    max_scatteredness: tuple
    scatteredness_warn: tuple
    neo_only: bool
    neo_q_max: float
    neo_e_min: float
    skip_already_observed: bool
    blacklist: tuple
    high_priority_surveys: tuple
    low_priority_surveys: tuple

    # --- Exposures ----------------------------------------------------------
    # Total integration, in minutes, from magnitude.
    exposure_base_min: float
    exposure_ref_mag: float
    exposure_min_per_mag: float
    exposure_floor_min: float
    # A separate quantity: the per-frame instruction, from speed.
    exposure_frames: int
    exposure_speed_bands: tuple
    exposure_fastest_sec: int

    # --- Ranking ------------------------------------------------------------
    default_sort: str
    rank_weights: dict
    arc_saturate_days: float
    mag_bright: float

    def __post_init__(self):
        """Freeze the sequence-valued settings.

        They are read on every cycle and two of them are cached as numpy
        arrays below, so a list that something appended to later would leave
        the cache and the setting disagreeing -- with no error.
        """
        self.horizon_mask = tuple(tuple(s) for s in self.horizon_mask)
        self.sector_names = tuple(self.sector_names)
        self.keepout_wedges = tuple(tuple(w) for w in self.keepout_wedges)
        self.max_scatteredness = tuple(self.max_scatteredness)
        self.scatteredness_warn = tuple(self.scatteredness_warn)
        self.blacklist = tuple(self.blacklist)
        self.high_priority_surveys = tuple(self.high_priority_surveys)
        self.low_priority_surveys = tuple(self.low_priority_surveys)
        self.exposure_speed_bands = tuple(tuple(b)
                                          for b in self.exposure_speed_bands)

    # --- Derived, cached ----------------------------------------------------

    @cached_property
    def min_alt_by_sector(self):
        """Mask floor per sector, as an array. inf means the whole sector is
        discouraged at every altitude."""
        import numpy as np
        # Indexed rather than unpacked: the entry grew a reason in stage D
        # and will grow again, and a positional unpack turns that into a
        # crash in the middle of an update cycle.
        return np.array([np.inf if e[2] is None else e[2]
                         for e in self.horizon_mask])

    @cached_property
    def hard_by_sector(self):
        """Whether violating a sector's limit refuses or merely warns."""
        import numpy as np
        return np.array([e[3] == "hard" for e in self.horizon_mask])

    @cached_property
    def earth_location(self):
        """The site as an astropy EarthLocation, from its parallax
        constants."""
        import numpy as np
        import astropy.units as u
        from astropy.coordinates import EarthLocation
        lon = np.radians(self.lon_deg)
        return EarthLocation.from_geocentric(
            self.rho_cos_phi * A_EARTH_M * np.cos(lon) * u.m,
            self.rho_cos_phi * A_EARTH_M * np.sin(lon) * u.m,
            self.rho_sin_phi * A_EARTH_M * u.m,
        )

    @cached_property
    def lat_deg(self):
        """Geodetic latitude, recovered from the parallax constants."""
        return float(self.earth_location.lat.deg)

    @cached_property
    def height_m(self):
        import astropy.units as u
        return float(self.earth_location.height.to(u.m).value)

    @cached_property
    def coords_text(self):
        """Where the telescope is, for the top of the board.

        MPC gives east longitude in 0..360; a site in the Americas would read
        as 280 degrees east rather than 80 west, which is correct and useless
        to a person, so it is folded to +-180 with a hemisphere letter.
        """
        lat = self.lat_deg
        lon = ((self.lon_deg + 180.0) % 360.0) - 180.0
        return (f"{abs(lat):.4f}°{'N' if lat >= 0 else 'S'} "
                f"{abs(lon):.5f}°{'E' if lon >= 0 else 'W'} "
                f"{self.height_m:.0f} m")

    def __repr__(self):
        return f"<Site {self.id} {self.obscode}>"


# --- loading sites from files ------------------------------------------------
#
# TOML, read with the standard library's tomllib. Deliberately a data format
# rather than Python: a site file describes an observatory, and nothing in it
# should be able to execute. The same reasoning runs through the plan-file
# templating decision in multisite.md -- user-supplied text is never code.

def _field_names():
    return [f.name for f in fields(Site)]


class SiteFileError(Exception):
    """A site file is missing, unreadable, or does not describe a site.

    Raised rather than warned about: a site whose horizon mask failed to load
    would still produce a board, and that board would be confidently wrong
    about where the telescope can point.
    """


def _as_tuples(value):
    """TOML gives arrays of arrays as lists of lists; the Site wants tuples."""
    return tuple(tuple(v) for v in value)


def from_dict(data, source="<dict>"):
    """Build a Site from a plain mapping, checking it is complete.

    Every field is required. There are no defaults on purpose: a site file
    that forgot to say what its magnitude limit is must fail loudly rather
    than quietly inherit somebody else's observatory's judgement.
    """
    names = _field_names()
    missing = [n for n in names if n not in data]
    unknown = [k for k in data if k not in names]
    if missing:
        raise SiteFileError(f"{source}: missing {', '.join(sorted(missing))}")
    if unknown:
        raise SiteFileError(f"{source}: unknown {', '.join(sorted(unknown))}")

    d = dict(data)
    # A mask entry's minimum altitude is optional in the sense that "no
    # altitude is good enough" is a real answer; TOML has no null, so a
    # sector writes -1 and means it.
    d["horizon_mask"] = tuple(
        (a, b, None if m is not None and m < 0 else m, h, r)
        for a, b, m, h, r in d["horizon_mask"])
    for key in ("keepout_wedges", "exposure_speed_bands"):
        d[key] = _as_tuples(d[key])
    for key in ("sector_names", "max_scatteredness", "scatteredness_warn",
                "blacklist", "high_priority_surveys", "low_priority_surveys"):
        d[key] = tuple(d[key])
    # Same convention for the one scalar that is genuinely optional.
    if d.get("max_altitude") is not None and d["max_altitude"] < 0:
        d["max_altitude"] = None
    try:
        return Site(**d)
    except TypeError as e:                     # pragma: no cover - defensive
        raise SiteFileError(f"{source}: {e}") from e


def load_file(path):
    """One site from one TOML file."""
    import tomllib
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise SiteFileError(f"{path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise SiteFileError(f"{path}: not valid TOML: {e}") from e
    return from_dict(data, source=path)


def load_dir(path):
    """Every *.toml in a directory, as {id: Site}, ordered by id.

    Ids and observatory codes must both be unique. A duplicate id would make
    two observatories share a board's worth of rows; a duplicate obscode
    would make the site switcher ambiguous and double our requests to MPC for
    the same ephemeris.
    """
    import glob

    out = {}
    by_code = {}
    for f in sorted(glob.glob(os.path.join(path, "*.toml"))):
        site = load_file(f)
        if site.id in out:
            raise SiteFileError(
                f"{f}: id {site.id} is already used by "
                f"{out[site.id].obscode}")
        if site.obscode in by_code:
            raise SiteFileError(
                f"{f}: obscode {site.obscode} is already used by site "
                f"{by_code[site.obscode].id}")
        out[site.id] = site
        by_code[site.obscode] = site
    if not out:
        raise SiteFileError(f"{path}: no site files found")
    return {k: out[k] for k in sorted(out)}
