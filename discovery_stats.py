"""Live tracker for per-observatory NEOCP success rates.

The question this answers: of the objects an observatory reports to the
NEOCP, what fraction turn out to be real (confirmed one way or another) vs.
not present at all (not confirmed / not a minor planet / does not exist /
suspected artificial)? NEO-vs-other-minor-planet is not distinguished --
"present" is the only thing that matters here.

Why this has to be built live rather than from history: the discovery
observatory is only readable from an object's 80-column astrometry (column
13 flags the discovery record, columns 78-80 carry the site -- see neocp.py),
and MPC's get-obs-neocp API only serves that astrometry while the object is
still on the live NEOCP page. Once an object is resolved and removed, that
API returns nothing for it, and MPC has no public bulk substitute (their own
docs point to a manual "request archival astrometry" form). So each object's
observatory has to be captured the moment it first appears, then watched
until its outcome shows up on the "previous NEOCP objects" archive page.

Run poll_once() periodically (see the cron entry alongside this file). Each
run is a handful of requests plus one extra request per newly-seen object;
MPC is a shared public service, so this mirrors the polling politeness of
update_neocp.py -- run it every 15 minutes, not continuously.
"""
import csv
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import neocp as neocp_mod          # the repo's own tested NEOCP-list parser
import observatories               # the repo's own code -> name lookup

_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_DIR, "discovery_stats.db")
CSV_PATH = os.path.join(_DIR, "observatory_stats.csv")
LOG_PATH = os.path.join(_DIR, "discovery_stats.log")

NEOCP_URL = "https://www.minorplanetcenter.net/iau/NEO/neocp.txt"
PREVDES_URL = "https://www.minorplanetcenter.net/iau/NEO/ToConfirm_PrevDes.html"
OBS_NEOCP_API = "https://data.minorplanetcenter.net/api/get-obs-neocp"

_UA = {"User-Agent": "whichneo_discovery_stats/0.1 (research use; contact aricall@uw.edu)"}
TIMEOUT = 30

# The archive's only four negative-outcome phrases (verified by scanning the
# full ~1800-entry current page -- see discovery_stats.md if that ever
# changes). Anything else after "=" is treated as a real, confirmed object,
# whether it's a brand new designation or a match to something already known.
_STATUS_MAP = {
    "was not confirmed": "not_confirmed",
    "was not a minor planet": "not_minor_planet",
    "does not exist": "does_not_exist",
    "was suspected artificial": "suspected_artificial",
}


def log(msg):
    with open(LOG_PATH, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def ensure_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS objects (
            desig TEXT PRIMARY KEY,
            obs_code TEXT,
            first_seen TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            resolved_at TEXT
        )
    """)
    con.commit()
    return con


def fetch_live_desigs():
    r = requests.get(NEOCP_URL, timeout=TIMEOUT, headers=_UA)
    r.raise_for_status()
    return [row["desig"] for row in neocp_mod.parse_neocp(r.text)]


def fetch_discovery_obs(desig):
    """Discovery observatory for a LIVE object, from its 80-column astrometry.
    Returns None once the object is off the live page -- see module docstring."""
    try:
        r = requests.get(OBS_NEOCP_API, timeout=TIMEOUT, headers=_UA,
                          json={"trksubs": [desig], "output_format": ["OBS80"]})
        r.raise_for_status()
        obs80 = r.json()[0].get("OBS80") or ""
    except Exception as e:
        log(f"obs lookup failed for {desig}: {e}")
        return None

    discovery, first = None, None
    for line in obs80.splitlines():
        if len(line) < 80:
            continue
        code = line[77:80].strip()
        if not code:
            continue
        if first is None:
            first = code
        if discovery is None and line[12] == "*":
            discovery = code
    return discovery or first


def fetch_prevdes_entries():
    """Parse the previous-NEOCP-objects archive into {desig: outcome}.

    outcome is "confirmed" (linked to any other identifier -- new designation,
    a merge into another tracklet, or a match to an already-known object all
    count as "the reported object is real") or one of the negative status
    keys above. The archive is newest-first and MPC sometimes revises an
    earlier verdict, so the first (i.e. most recent) entry for a given desig
    wins; later, older duplicates are ignored.
    """
    r = requests.get(PREVDES_URL, timeout=TIMEOUT, headers=_UA)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    anchor = soup.find("a", attrs={"name": "prev"})
    ul = anchor.find_next("ul") if anchor else None
    if ul is None:
        return {}

    out = {}
    for li in ul.find_all("li", recursive=False):
        # No separator: a <sub> tag (e.g. "RZ<sub>34</sub>") must concatenate
        # directly onto "RZ34" with no inserted space. Collapse the source's
        # own whitespace/newlines afterward instead.
        text = re.sub(r"\s+", " ", li.get_text()).strip()
        if "(" not in text:
            continue
        body = text.split("(", 1)[0].strip()
        if " = " in body:
            left, right = (t.strip() for t in body.split(" = ", 1))
            out.setdefault(left, "confirmed")
            out.setdefault(right, "confirmed")
        else:
            parts = body.split(None, 1)
            if len(parts) != 2:
                continue
            desig, phrase = parts
            outcome = _STATUS_MAP.get(phrase.strip())
            if outcome:
                out.setdefault(desig, outcome)
    return out


def poll_once():
    con = ensure_db()
    now = datetime.now(timezone.utc).isoformat()

    live = fetch_live_desigs()
    known = {row[0] for row in con.execute("SELECT desig FROM objects")}
    new_desigs = [d for d in live if d not in known]

    for d in new_desigs:
        obs_code = fetch_discovery_obs(d)
        con.execute(
            "INSERT INTO objects (desig, obs_code, first_seen, status) "
            "VALUES (?, ?, ?, 'pending')",
            (d, obs_code, now))
        log(f"new object {d} obs_code={obs_code}")
    con.commit()

    pending = [row[0] for row in
               con.execute("SELECT desig FROM objects WHERE status='pending'")]
    if pending:
        resolutions = fetch_prevdes_entries()
        for d in pending:
            outcome = resolutions.get(d)
            if outcome:
                con.execute(
                    "UPDATE objects SET status=?, resolved_at=? WHERE desig=?",
                    (outcome, now, d))
                log(f"resolved {d} -> {outcome}")
        con.commit()

    con.close()
    write_csv()


def write_csv():
    con = ensure_db()
    rows = con.execute("""
        SELECT obs_code, status, COUNT(*) FROM objects
        WHERE obs_code IS NOT NULL AND status != 'pending'
        GROUP BY obs_code, status
    """).fetchall()
    con.close()

    stats = {}
    for obs_code, status, n in rows:
        s = stats.setdefault(obs_code, {"reported": 0, "confirmed": 0})
        s["reported"] += n
        if status == "confirmed":
            s["confirmed"] += n

    with open(CSV_PATH, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["obs_code", "observatory_name", "reported", "confirmed",
                    "success_rate"])
        for obs_code, s in sorted(stats.items(), key=lambda kv: -kv[1]["reported"]):
            name = observatories.site_name(obs_code) or ""
            rate = s["confirmed"] / s["reported"] if s["reported"] else 0.0
            w.writerow([obs_code, name, s["reported"], s["confirmed"], f"{rate:.3f}"])


if __name__ == "__main__":
    poll_once()
