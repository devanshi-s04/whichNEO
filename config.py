"""Configuration for visnjan_whichneo.

Every observatory-specific rule lives here. Values marked TBD are placeholders
awaiting confirmation from Luka -- see TBD.md for the open questions.
"""

# --- Site: L01, Ticio/Tican Station, Visnjan Observatory ---------------------
# Derived from the official MPC parallax constants rather than a hardcoded
# lat/lon, so the site matches exactly what Find_Orb and the MPC use for L01.
# ObsCodes.htm: "L01  13.749300.704742+0.707169Visnjan Observatory, Tican"
# These resolve to 45.2909 N, 13.74930 E, 381 m.
MPC_CODE = "L01"
SITE_LON_DEG = 13.74930
SITE_RHO_COS_PHI = 0.704742
SITE_RHO_SIN_PHI = 0.707169

# --- NEOCP source ------------------------------------------------------------
NEOCP_URL = "https://www.minorplanetcenter.net/iau/NEO/neocp.txt"
NEOCP_TIMEOUT_S = 30

# Only these prefixes may be fetched for on-demand ephemerides. Guards against
# following an arbitrary URL; the legacy MPCS tool does the same thing.
ALLOWED_HOSTS = (
    "https://www.minorplanetcenter.net/",
    "https://cgi.minorplanetcenter.net/",
)

# --- Horizon / dome mask -----------------------------------------------------
# Azimuth sector -> minimum observable altitude (deg). None means blocked.
# Sectors are (az_start, az_end] going clockwise; N wraps through 0.
#
# TBD -- taken from observer notes which contain two unresolved conflicts:
#   * "northwest is 50 degrees" vs later "Northwest = 40"  -> using 40
#   * "below 30-40 degrees in the west" vs "West = 40"     -> using 40
#   * SW was never specified                               -> interpolated 30
# Confirm all of these with Luka before trusting the observable/unobservable
# flag. Replace wholesale once the real dome geometry is available.
HORIZON_MASK = [
    (337.5, 22.5, None),   # N  - "avoid north completely"
    (22.5, 67.5, 20.0),    # NE - "above 20 in South and East"
    (67.5, 112.5, 20.0),   # E
    (112.5, 157.5, 20.0),  # SE
    (157.5, 202.5, 20.0),  # S
    (202.5, 247.5, 30.0),  # SW - TBD, interpolated between S and W
    (247.5, 292.5, 40.0),  # W  - "West = 40"
    (292.5, 337.5, 40.0),  # NW - "Northwest = 40"
]

# Zenith blind spot. Observer mentioned an "upper elevation" limit but gave no
# value; common on fork mounts. None disables the check.
MAX_ALTITUDE = None  # TBD

# --- Observability limits ----------------------------------------------------
MAX_MAG = 21.7
# Sun must be below this for a target to count as observable.
SUN_ALT_MAX = -12.0  # nautical dark
MOON_SEP_MIN = 20.0  # TBD -- observer said not to over-engineer the Moon rule

# --- Exposures ---------------------------------------------------------------
# TBD -- real exposure rules unknown. 30 s per frame was specified by the
# observer. Count defaults to 6 because that preserves the 180 s total from
# the "36 x 05 sec" example in the original notes. Both need confirming, and
# the real rule almost certainly depends on target magnitude and sky motion.
EXPOSURE_SECONDS = 30
EXPOSURE_COUNT = 6

# --- Ranking -----------------------------------------------------------------
# Intrinsic follow-up value only: deliberately excludes altitude so the
# ordering is stable 24/7. Observability is a separate flag that sinks
# unobservable targets below observable ones at sort time.
RANK_WEIGHTS = {
    "digest2": 2.0,
    "arc": 1.5,
    "magnitude": 1.5,
}
# An arc at or beyond this many days scores zero for urgency.
ARC_SATURATE_DAYS = 3.0
# Magnitude normalisation window: MAG_BRIGHT scores 1.0, MAX_MAG scores 0.0.
MAG_BRIGHT = 15.0

# --- Survey priorities (TBD) -------------------------------------------------
# The tabular NEOCP feed does not carry the discovering survey. It can only be
# inferred from the designation prefix, which is a heuristic. Left empty until
# we decide whether it is worth fetching per-object observation data.
HIGH_PRIORITY_SURVEYS = []  # TBD
LOW_PRIORITY_SURVEYS = []   # TBD
BLACKLIST = []              # TBD

# --- Storage / runtime -------------------------------------------------------
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
# data/ is gitignored, so a fresh clone has no such directory. Create it at
# import time: logging opens its file before anything else touches the path.
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "targets.db")
LOG_PATH = os.path.join(DATA_DIR, "update.log")
UPDATE_INTERVAL_S = 300
WEB_POLL_INTERVAL_S = 20
