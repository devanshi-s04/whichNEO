"""Per-object ephemerides from the MPC confirmation-page CGI.

MPC generates these for our site directly (obscode=L01), which is where sky
motion, moon distance and solar altitude come from -- none of which appear in
the NEOCP list feed. One request returns a whole night for one object.

The legacy planner fetches an object's ephemeris once, the first time it is
seen, and never again. That is why a target rejected early in the evening for
being too low stays rejected after it has risen. Here the fetch is cached
against a signature of the object's NEOCP row, so it repeats only when new
observations actually change the solution.
"""

import calendar
import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

import config

_UA = {"User-Agent": "visnjan_whichneo/0.2 "
                     "(Visnjan Observatory L01 follow-up planning)"}

# Version of the cached payload's CONTENT, not its shape. Bump it whenever a
# parser changes what we would extract from the same page, so every cached
# entry is refetched once.
#
# Without this a parser fix never reaches objects already cached: an entry is
# only refreshed when its signature changes, and the signature tracks new
# astrometry, not our own bugs. Entries would keep serving the wrong result
# until the object happened to be observed again.
#
#   2 -- offsets regex now tolerates MPC's trailing ! / !! motion flag, which
#        had been silently discarding the uncertainty cloud of every fast
#        mover.
CACHE_SCHEMA = 2

# Row layout, whitespace separated:
#   0    1  2   3      4  5   6    7   8  9   10     11     12     13    14   15   16    17    18   19
#   2026 09 10 2000   02 30 34.7 +36 47 19  118.3   16.4  391.9  047.2  240  +23  -26  0.00   115  -28
#   <-----date----->  <--R.A.--> <-Decl.->  Elong.     V  "/min   P.A.  Azi  Alt  Sun Phase  Dist  Alt
_I_ELONG, _I_V, _I_MOTION, _I_PA = 10, 11, 12, 13
_I_AZ, _I_ALT, _I_SUNALT, _I_PHASE, _I_MOONDIST, _I_MOONALT = 14, 15, 16, 17, 18, 19
_MIN_FIELDS = 20


def _to_unix_utc(y, mo, d, hhmm):
    """Ephemeris timestamps are UTC. The legacy code pushes them through
    time.mktime(), which reinterprets them as local time; the error largely
    cancels because it does the same to 'now', but it is wrong on its face."""
    return calendar.timegm(
        dt.datetime(int(y), int(mo), int(d), int(hhmm[:2]), int(hhmm[2:])).timetuple())


class Row:
    """One ephemeris line."""

    __slots__ = ("line", "ts", "ra_deg", "dec_deg", "elong", "vmag", "motion",
                 "pa", "az", "az_mpc", "alt", "sun_alt", "moon_phase",
                 "moon_dist", "moon_alt", "flag")

    def __init__(self, line):
        p = line.split()
        if len(p) < _MIN_FIELDS:
            raise ValueError("short ephemeris row")
        self.line = line.rstrip()
        self.ts = _to_unix_utc(p[0], p[1], p[2], p[3])

        self.ra_deg = (float(p[4]) + float(p[5]) / 60 + float(p[6]) / 3600) * 15.0
        sign = -1.0 if p[7].startswith("-") else 1.0
        self.dec_deg = sign * (abs(float(p[7])) + float(p[8]) / 60 + float(p[9]) / 3600)

        self.elong = float(p[_I_ELONG])
        self.vmag = float(p[_I_V])
        self.motion = float(p[_I_MOTION])
        self.pa = float(p[_I_PA])
        # MPC reports azimuth measured from SOUTH; everything else here (the
        # dome mask, the sky map, astropy) uses the compass convention from
        # north. Verified against astropy: MPC 228.0 vs ours 48.2, MPC 244.5
        # vs ours 64.6, with altitudes agreeing to better than half a degree.
        # The legacy planner carries the same +180 in its sky-map code.
        self.az_mpc = float(p[_I_AZ])
        self.az = (self.az_mpc + 180.0) % 360.0
        self.alt = float(p[_I_ALT])
        self.sun_alt = float(p[_I_SUNALT])
        self.moon_phase = float(p[_I_PHASE])
        self.moon_dist = float(p[_I_MOONDIST])
        self.moon_alt = float(p[_I_MOONALT])
        stripped = self.line.rstrip()
        self.flag = "!!" if stripped.endswith("!!") else (
            "!" if stripped.endswith("!") else "")

    @property
    def utc(self):
        return dt.datetime.fromtimestamp(self.ts, dt.timezone.utc)

    def exposure_minutes(self):
        """The observatory's own rule, recovered from the legacy planner.

        Clamped at the floor: the formula is unbounded below and returns
        negative minutes for anything brighter than about V=16.
        """
        mins = (config.EXPOSURE_BASE_MIN
                + (self.vmag - config.EXPOSURE_REF_MAG)
                * config.EXPOSURE_MIN_PER_MAG)
        return round(max(mins, config.EXPOSURE_FLOOR_MIN), 2)

    def frame_seconds(self):
        """Seconds per frame for this row's sky motion, from the observatory's
        own table in config.EXPOSURE_SPEED_BANDS.

        The faster an object moves, the shorter each frame must be to keep it
        from trailing across the detector. Bounds are upper-inclusive.
        """
        for limit, seconds in config.EXPOSURE_SPEED_BANDS:
            if self.motion <= limit:
                return seconds
        return config.EXPOSURE_FASTEST_SEC

    def frame_plan(self):
        """(frames, seconds) -- the instruction for this row."""
        return config.EXPOSURE_FRAMES, self.frame_seconds()

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


def track(lines):
    """Just (ts, compass azimuth, altitude) for each parseable line.

    The sky map redraws on every page load so its positions are current
    rather than up to five minutes stale, which means re-reading the cached
    ephemeris of every observable target on each request. Building full Row
    objects for that is wasted work -- the map needs three numbers per line.

    It deliberately reuses the same field indices and the same +180 azimuth
    conversion as Row rather than repeating them: a second, drifting copy of
    the south-versus-north convention is exactly the bug that would rotate
    the whole map through half a turn without anything failing.
    """
    out = []
    for line in lines:
        p = line.split()
        if len(p) < _MIN_FIELDS:
            continue
        try:
            out.append((_to_unix_utc(p[0], p[1], p[2], p[3]),
                        (float(p[_I_AZ]) + 180.0) % 360.0,
                        float(p[_I_ALT])))
        except (ValueError, IndexError):
            continue
    out.sort()
    return out


class ObjectEphemeris:
    def __init__(self, desig, rows, offsets_url=None, map_url=None,
                 observations_url=None, error=None):
        self.desig = desig
        self.rows = rows
        self.offsets_url = offsets_url
        self.map_url = map_url
        self.observations_url = observations_url
        self.error = error

    def __bool__(self):
        return bool(self.rows)

    def max_altitude_row(self):
        return max(self.rows, key=lambda r: r.alt) if self.rows else None

    def nearest_to(self, ts):
        return min(self.rows, key=lambda r: abs(r.ts - ts)) if self.rows else None

    def interpolate_at(self, ts):
        """Linear alt/az interpolation between the bracketing rows.

        The legacy planner evaluates at now + 600 s so the coordinates account
        for slew time; we keep that, but rebuild the output line from the data
        instead of splicing the original string, which corrupts it.
        """
        if not self.rows:
            return None
        rows = sorted(self.rows, key=lambda r: r.ts)
        if ts <= rows[0].ts:
            return rows[0]
        if ts >= rows[-1].ts:
            return rows[-1]
        for a, b in zip(rows, rows[1:]):
            if a.ts <= ts <= b.ts:
                span = b.ts - a.ts
                f = 0.0 if span == 0 else (ts - a.ts) / span
                out = Row(a.line)
                out.ts = ts
                out.alt = a.alt + (b.alt - a.alt) * f
                # Azimuth wraps at 360; interpolate the short way round.
                d_az = ((b.az - a.az + 180) % 360) - 180
                out.az = (a.az + d_az * f) % 360
                out.az_mpc = (out.az - 180.0) % 360.0
                out.ra_deg = a.ra_deg + (((b.ra_deg - a.ra_deg + 180) % 360) - 180) * f
                out.dec_deg = a.dec_deg + (b.dec_deg - a.dec_deg) * f
                out.vmag = a.vmag + (b.vmag - a.vmag) * f
                out.motion = a.motion + (b.motion - a.motion) * f
                out.sun_alt = a.sun_alt + (b.sun_alt - a.sun_alt) * f
                out.moon_dist = a.moon_dist + (b.moon_dist - a.moon_dist) * f
                return out
        return rows[-1]


def from_lines(desig, lines, offsets_url=None, map_url=None,
               observations_url=None):
    """Rebuild from cached raw lines. Re-parsing is cheaper and far less
    fragile than serialising the parsed objects."""
    rows = []
    for line in lines:
        try:
            rows.append(Row(line))
        except (ValueError, IndexError):
            continue
    return ObjectEphemeris(desig, rows, offsets_url, map_url, observations_url)


def signature(target):
    """Changes only when new astrometry has altered the solution, which is
    the only reason to re-request an ephemeris.

    Deliberately excludes not_seen_days. That field is the age of the last
    observation, so it advances with the wall clock even when nothing about
    the object has changed -- including it invalidated every cached
    ephemeris on every cycle, which silently disabled the cache entirely and
    turned a two-second update into eighty.
    """
    return f"{target['nobs']}|{target['arc_days']}"


def _post(desig, timeout=None):
    return requests.post(
        config.EPHEMERIS_URL,
        data={"mb": -30, "mf": 30, "dl": -90, "du": 90, "nl": 0, "nu": 100,
              "sort": "d", "W": "j", "obj": desig, "Parallax": 1,
              "obscode": config.MPC_CODE, "int": 1, "start": 0, "raty": "a",
              "mot": "m", "dmot": "p", "out": "f", "sun": "x",
              "oalt": int(config.MPC_SERVER_MIN_ALT)},
        timeout=timeout or config.EPHEMERIS_TIMEOUT_S, headers=_UA)


def parse(desig, html_text):
    """Extract ephemeris rows and the auxiliary links from one CGI response."""
    soup = BeautifulSoup(html_text, "lxml")
    pre = soup.find("pre")
    if pre is None:
        return ObjectEphemeris(desig, [], error="no <pre> in response")

    rows = []
    for line in pre.get_text().split("\n"):
        if "<suppressed>" in line:
            continue
        try:
            rows.append(Row(line))
        except (ValueError, IndexError):
            continue  # header and decoration lines

    offsets_url = map_url = None
    for a in pre.find_all("a"):
        label = a.get_text(strip=True)
        if label == "Offsets" and offsets_url is None:
            offsets_url = a.get("href")
        elif label == "Map" and map_url is None:
            map_url = a.get("href")
        if offsets_url and map_url:
            break

    obs_url = None
    for a in soup.find_all("a"):
        if a.get_text(strip=True) == "observations":
            obs_url = a.get("href")
            break

    return ObjectEphemeris(desig, rows, offsets_url, map_url, obs_url)


def fetch(desig):
    """Fetch and parse one object's ephemeris. Never raises."""
    try:
        r = _post(desig)
        r.raise_for_status()
        return parse(desig, r.text)
    except Exception as e:
        return ObjectEphemeris(desig, [], error=f"{type(e).__name__}: {e}")


def _post_gap_fill(desig, timeout=None):
    """Same request as _post(), but with no altitude floor at all.

    Scoped deliberately: this exists only to fill holes in moonplot.py's
    chart when the object dips under MPC_SERVER_MIN_ALT for part of the
    night (see update_neocp.py's _aux and app.py's target_detail, the only
    two callers of this and fetch_gap_fill below). The filter cascade, the
    sky map, and the uncertainty plot all keep reading the normal oalt=20
    fetch untouched, so loosening the floor here changes nothing about what
    gets filtered, ranked, or drawn anywhere else.
    """
    return requests.post(
        config.EPHEMERIS_URL,
        data={"mb": -30, "mf": 30, "dl": -90, "du": 90, "nl": 0, "nu": 100,
              "sort": "d", "W": "j", "obj": desig, "Parallax": 1,
              "obscode": config.MPC_CODE, "int": 1, "start": 0, "raty": "a",
              "mot": "m", "dmot": "p", "out": "f", "sun": "x", "oalt": -90},
        timeout=timeout or config.EPHEMERIS_TIMEOUT_S, headers=_UA)


def fetch_gap_fill(desig):
    """The same object's ephemeris with no altitude floor. Never raises;
    an empty ObjectEphemeris on failure, exactly like fetch()."""
    try:
        r = _post_gap_fill(desig)
        r.raise_for_status()
        return parse(desig, r.text)
    except Exception as e:
        return ObjectEphemeris(desig, [], error=f"{type(e).__name__}: {e}")


def fetch_many(desigs, workers=None):
    """Fetch several objects concurrently, politely."""
    desigs = list(desigs)
    if not desigs:
        return {}
    with ThreadPoolExecutor(
            max_workers=workers or config.EPHEMERIS_WORKERS) as pool:
        return {e.desig: e for e in pool.map(fetch, desigs)}


# --- auxiliary pages -------------------------------------------------------

# The trailing `\s*[!]*\s*$` is the whole point. MPC appends its fast-motion
# flag AFTER the ephemeris number:
#
#     +6581   +4822      Ephemeris #    2 !!
#
# and the previous pattern anchored `$` immediately after the digits, so every
# line of a fast mover's page failed to match and the object was recorded as
# having no uncertainty data at all. Measured on a live board that was 40 of
# 103 objects, with a median motion of 17.6 "/min against 2.4 for those that
# parsed -- so it stripped the uncertainty from precisely the objects whose
# uncertainty matters most. ZTF10G9's page held 2000 real variants reaching
# 11175 arcsec from the nominal position; we read none of them.
#
# Same failure as the ObsCodes parser: a regex anchored to end-of-line, broken
# by an optional trailing token, failing silently rather than raising.
_OFFSET_RE = re.compile(
    r"([+-][0-9]+)\s+([+-][0-9]+).*?Ephemeris #\s*[0-9]+\s*!*\s*$", re.M)


def offsets(offsets_url):
    """The variant-orbit offsets behind MPC's uncertainty map.

    Roughly 2000 (dRA, dDec) pairs in arcseconds from the nominal solution --
    the points MPC plots. We already pay for this page to compute
    scatteredness, so keeping the points costs no extra request and lets the
    board draw the map itself.
    """
    if not offsets_url:
        return None
    try:
        r = requests.get(offsets_url, timeout=config.NEOCP_TIMEOUT_S, headers=_UA)
        r.raise_for_status()
        pre = BeautifulSoup(r.text, "lxml").find("pre")
        if pre is None:
            return None
        pts = [(int(a), int(b)) for a, b in _OFFSET_RE.findall(pre.get_text())]
        return pts or None
    except Exception:
        return None


def spread(points):
    """Extent of the uncertainty cloud in arcseconds, as (dRA, dDec).

    Large values mean the predicted position is smeared over more sky than a
    single pointing can cover.
    """
    if not points:
        return None
    ras = [a for a, _ in points]
    decs = [b for _, b in points]
    return (max(ras) - min(ras), max(decs) - min(decs))


def scatteredness(offsets_url):
    """Backwards-compatible helper: fetch and reduce in one step."""
    return spread(offsets(offsets_url))


def observations(observations_url, mpc_code=None):
    """Who has observed this object, from its 80-column astrometry.

    One fetch answers two questions: whether our own site is already among
    the observers, and which observatory discovered it. Column 13 carries an
    asterisk on the discovery record; columns 78-80 are the observatory code.
    The format records no individual observer, only the site.
    """
    if not observations_url:
        return None
    code = mpc_code or config.MPC_CODE
    try:
        r = requests.get(observations_url, timeout=config.NEOCP_TIMEOUT_S,
                         headers=_UA)
        r.raise_for_status()
        pre = BeautifulSoup(r.text, "lxml").find("pre")
        text = pre.get_text() if pre else r.text
    except Exception:
        return None

    return parse_observations(text, code)


def parse_observations(text, mpc_code=None):
    """Split out from the fetch so it can be tested without the network."""
    code = mpc_code or config.MPC_CODE
    counts, discovery, first = {}, None, None
    for line in text.splitlines():
        if len(line) < 80:
            continue
        obscode = line[77:80].strip()
        if not obscode:
            continue
        counts[obscode] = counts.get(obscode, 0) + 1
        if first is None:
            first = obscode
        if discovery is None and line[12] == "*":
            discovery = obscode

    if not counts:
        return None
    return {"codes": counts,
            # Records are in time order, so the earliest stands in when no
            # discovery asterisk is present.
            "discovery_code": discovery or first,
            "observed_from_site": code in counts}


def observed_from_site(observations_url, mpc_code=None):
    """Backwards-compatible helper."""
    summary = observations(observations_url, mpc_code)
    return summary["observed_from_site"] if summary else None
