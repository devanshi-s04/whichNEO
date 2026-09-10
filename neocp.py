"""Fetch and parse the MPC NEO Confirmation Page tabular feed."""

import re
import requests

import config

# Columns in neocp.txt, in order:
#   desig  score  Y M D.d  RA(hours)  Dec(deg)  V  <"Added|Updated <date> UT">
#   NObs  Arc(days)  H  NotSeen(days)
#
# The note field in the middle is variable width, so the leading eight fields
# are read from the front and the trailing four from the back.
_MIN_TOKENS = 12

# Designation prefix -> survey. Heuristic only: the tabular feed does not carry
# the discovering observatory, so this is inferred and may be wrong.
_SURVEY_PREFIXES = [
    ("P1", "Pan-STARRS"),
    ("P2", "Pan-STARRS"),
    ("A10", "ATLAS"),
    ("A11", "ATLAS"),
    ("ZTF", "ZTF"),
    ("C1", "Catalina"),
    ("C2", "Catalina"),
    ("CER", "Cerro Tololo"),
]


def guess_survey(desig):
    for prefix, name in _SURVEY_PREFIXES:
        if desig.startswith(prefix):
            return name
    return None


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
                digest2=int(tok[1]),
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
        # "Added" marks a first posting, "Updated" a re-posting -- a free
        # recency signal distinct from the discovery date.
        row["is_new"] = note.strip().lower().startswith("added")
        row["survey"] = guess_survey(row["desig"])
        rows.append(row)
    return rows


def fetch_neocp(url=None, timeout=None):
    """Download the live NEOCP list. Raises on HTTP or network failure."""
    url = url or config.NEOCP_URL
    timeout = timeout or config.NEOCP_TIMEOUT_S
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": "visnjan_whichneo/0.1 (Visnjan Observatory L01 follow-up planning)"
    })
    r.raise_for_status()
    return r.text


def ephemeris_url(desig):
    """MPC ephemeris CGI URL for one NEOCP object, generated for L01."""
    return (
        "https://cgi.minorplanetcenter.net/cgi-bin/confirmeph2.cgi"
        f"?Obj={desig}&obscode={config.MPC_CODE}"
    )


def fetch_ephemeris(desig, timeout=30):
    """On-demand per-object ephemeris. Only called when an observer opens a
    target, so the 5-minute update loop stays a single HTTP request."""
    url = ephemeris_url(desig)
    if not url.startswith(config.ALLOWED_HOSTS):
        raise ValueError(f"refusing to fetch disallowed URL: {url}")
    r = requests.get(url, timeout=timeout, headers={
        "User-Agent": "visnjan_whichneo/0.1 (Visnjan Observatory L01 follow-up planning)"
    })
    r.raise_for_status()
    return r.text
