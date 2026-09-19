"""Fetch and parse the MPC NEO Confirmation Page tabular feed."""

import re
import requests

import config

_UA = {"User-Agent": "visnjan_whichneo/0.2 "
                     "(Visnjan Observatory L01 follow-up planning)"}

# Columns in neocp.txt, in order:
#   desig  score  Y M D.d  RA(hours)  Dec(deg)  V  <"Added|Updated <date> UT">
#   NObs  Arc(days)  H  NotSeen(days)
#
# The note field in the middle is variable width, so the leading eight fields
# are read from the front and the trailing four from the back.
_MIN_TOKENS = 12

# The discovering observatory used to be guessed from the designation prefix.
# That heuristic is gone: the real observatory code is read from the object's
# astrometry, where column 13 marks the discovery record and columns 78-80
# carry the site. See ephemeris.observations().


def parse_neocp(text):
    """Parse neocp.txt content into a list of dicts."""
    rows = []
    for line in text.splitlines():
        tok = line.split()
        if len(tok) < _MIN_TOKENS:
            continue
        try:
            row = dict(
                desig=tok[0],
                score=int(tok[1]),  # MPC's digest2 NEO score
                disc_year=int(tok[2]),
                disc_month=int(tok[3]),
                disc_day=float(tok[4]),
                ra_deg=float(tok[5]) * 15.0,
                dec_deg=float(tok[6]),
                vmag=float(tok[7]),
                nobs=int(tok[-4]),
                arc_days=float(tok[-3]),
                hmag=float(tok[-2]),
                not_seen_days=float(tok[-1]),
            )
        except ValueError:
            continue

        note = " ".join(tok[8:-4])
        row["update_note"] = note
        # MPC's Note column, carried at the end of the note text. Both values
        # are warnings, not endorsements: "S" means the object is possibly in
        # geocentric orbit (so probably a satellite, and due for removal),
        # "B" that the tracklet or orbit fit may be bad and the ephemeris
        # unreliable. Surfaced so an observer can judge; never used to promote.
        row["note_flag"] = note.split()[-1] if (
            note and len(note.split()[-1]) == 1 and note.split()[-1].isupper()
        ) else None
        # "Added" marks a first posting, "Updated" a re-posting -- a free
        # recency signal distinct from the discovery date.
        row["is_new"] = note.strip().lower().startswith("added")
        rows.append(row)
    return rows


def fetch_neocp(url=None, timeout=None):
    """Download the live NEOCP list. Raises on HTTP or network failure."""
    url = url or config.NEOCP_URL
    timeout = timeout or config.NEOCP_TIMEOUT_S
    r = requests.get(url, timeout=timeout, headers=_UA)
    r.raise_for_status()
    return r.text


# --- neocp_info: orbital parameters ----------------------------------------
#
# Fixed-width table, e.g.
#   desig        H   ra dec  V  elong  arc  gap    i    e       a  used/nobs rms C
#   6J93321    23.5  19 -40 18.9 119  0.14  0.5  11.2 0.316   1.093  12/12  0.27
#
# Both e and a are printed with exactly three decimals, and when a is large the
# two columns run together with no separating space:
#   A11GrOJ    10.1   1 -32 19.7 142  0.03  1.7 126.0 0.9982411.744   4/4  0.37
#                                                     ^^^^^^^^^^^^ e=0.998 a=2411.744
# Anchoring on the three-decimal shape recovers those rows. Splitting on
# whitespace and requiring 13 fields — what the legacy planner does — silently
# drops them, and they are exactly the extreme orbits worth looking at.
_INFO_RE = re.compile(
    r"^(?P<desig>\S+)\s+(?P<H>-?[\d.]+)\s+(?P<ra>-?\d+)\s+(?P<dec>-?\d+)\s+"
    r"(?P<V>[\d.]+)\s+(?P<elong>\d+)\s+(?P<arc>[\d.]+)\s+(?P<gap>[\d.]+)\s+"
    r"(?P<i>[\d.]+)\s+(?P<e>\d+\.\d{3})\s*(?P<a>\d+\.\d{3})\s+"
    r"(?P<used>\d+)/(?P<nobs>\d+)\s+(?P<rms>[\d.]+)"
)


def parse_neocp_info(text):
    """Parse the neocp_info table into {designation: {q, e, a, i, ...}}."""
    out = {}
    for line in text.splitlines():
        line = line.split("<a href")[0].rstrip()
        m = _INFO_RE.match(line)
        if not m:
            continue
        g = m.groupdict()
        e, a = float(g["e"]), float(g["a"])
        q = a * (1.0 - e)
        out[g["desig"]] = dict(
            q=q if q > 0 else None,  # a*(1-e) is meaningless for e >= 1
            e=e, a=a, incl=float(g["i"]), abs_mag=float(g["H"]),
            used=int(g["used"]), nobs_info=int(g["nobs"]), rms=float(g["rms"]),
        )
    return out


def fetch_neocp_info(timeout=None):
    """Orbital parameters for everything currently on NEOCP. One request."""
    r = requests.get(config.NEOCP_INFO_URL,
                     timeout=timeout or config.NEOCP_TIMEOUT_S,
                     headers=_UA)
    r.raise_for_status()
    return r.text


def ephemeris_url(desig, site=None):
    """MPC ephemeris CGI URL for one NEOCP object, generated for a site."""
    obscode = (config.DEFAULT_SITE if site is None else site).obscode
    return (
        "https://cgi.minorplanetcenter.net/cgi-bin/confirmeph2.cgi"
        f"?Obj={desig}&obscode={obscode}"
    )


def fetch_ephemeris(desig, timeout=30, site=None):
    """On-demand per-object ephemeris. Only called when an observer opens a
    target, so the 5-minute update loop stays a single HTTP request."""
    url = ephemeris_url(desig, site)
    if not url.startswith(config.ALLOWED_HOSTS):
        raise ValueError(f"refusing to fetch disallowed URL: {url}")
    r = requests.get(url, timeout=timeout, headers=_UA)
    r.raise_for_status()
    return r.text


# --- MPC's variant-orbit population scores -----------------------------------
#
# An hourly table at NEOCP_CLASSES_URL, one row per object:
#
#   desig digest2 NEO large_e MC HUN MB HIL JFC TRO DIST H arc Nsets Unc V
#   dq de di
#
# Two columns earn their place here. MB is MPC's own main-belt score, which is
# what the uncertainty map's "dark blue" means and which MPC nowhere defines
# numerically -- using theirs beats inventing an a/e cut and calling it theirs.
# H is a median over the variant orbits, and it disagrees with neocp.txt's H by
# about half a magnitude, which is a quarter of the derived distance.
#
# MPC marks the page Beta and publishes no JSON or CSV, so this reads the table
# by its own header row rather than by column position: a column inserted
# upstream then moves nothing. Every field is optional. The board worked before
# this table existed and has to keep working if it goes away.

_CLASS_CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_CLASS_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_CLASS_TAG = re.compile(r"<[^>]+>")

# Deliberately narrow. The rest of the table is real data we have no use for
# yet, and parsing what we do not use is surface area for no gain.
_CLASS_WANT = {"H": "mpc_h", "MB": "mb_score", "TRO": "tro_score",
               "NEO": "neo_score", "Unc": "sky_unc_sqdeg"}


def parse_neocp_classes(html):
    """{designation: {mpc_h, mb_score, tro_score, neo_score, ...}}.

    A missing or unparseable cell comes back absent rather than as zero: a
    main-belt score nobody supplied is not the same fact as a score of 0.
    """
    out = {}
    rows = _CLASS_ROW.findall(html or "")
    if not rows:
        return out
    header = [_CLASS_TAG.sub("", c).strip()
              for c in _CLASS_CELL.findall(rows[0])]
    if not header or header[0].lower() not in ("desig", "designation"):
        return out
    for row in rows[1:]:
        cells = [_CLASS_TAG.sub("", c).strip()
                 for c in _CLASS_CELL.findall(row)]
        if len(cells) != len(header):
            continue
        by_name = dict(zip(header, cells))
        desig = by_name.get(header[0])
        if not desig:
            continue
        entry = {}
        for column, field in _CLASS_WANT.items():
            raw = by_name.get(column)
            if raw in (None, "", "n.a."):
                continue
            try:
                entry[field] = float(raw)
            except ValueError:
                continue
        if entry:
            out[desig] = entry
    return out


def fetch_neocp_classes(timeout=None):
    """The variant-orbit table for everything on NEOCP. One request."""
    r = requests.get(config.NEOCP_CLASSES_URL,
                     timeout=timeout or config.NEOCP_TIMEOUT_S, headers=_UA)
    r.raise_for_status()
    return r.text
