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

from dataclasses import dataclass
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
    # (az_start, az_end, minimum observable altitude or None, hardness).
    # "soft" keeps the target and flags it; "hard" rejects it outright. See
    # config.py for why L01's are all soft.
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
        return np.array([np.inf if m is None else m
                         for (_, _, m, _h) in self.horizon_mask])

    @cached_property
    def hard_by_sector(self):
        """Whether violating a sector's limit refuses or merely warns."""
        import numpy as np
        return np.array([h == "hard" for (_, _, _m, h) in self.horizon_mask])

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

    def __repr__(self):
        return f"<Site {self.id} {self.obscode}>"
